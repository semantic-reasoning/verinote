"""Bounded extraction orchestration; validation never performs human approval."""
import copy
import json
import re
import shutil
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path

from .agents import CLI, PROVIDERS, validate_model
from .call_validation import caller_validation
from .dictionary_context import RequestTooLarge, refusal, validate_context_bytes
from .dictionary_context import prepare as prepare_dictionary_prompt
from .documents import chunks
from .engine import query
from .extraction_checkpoint import CHUNK_TIMEOUT, ExtractionCheckpoint
from .extraction_coverage import (
    chunk_report,
    exclude_off_dictionary_facts,
    locate_whitespace_variants,
    located_quote_notice,
    new_report,
    read_report,
    repair_missing_indices,
    resolve_quote_repairs,
    saved_quote_paths,
    write_located_quotes,
    write_repair_audit,
    write_report,
)
from .ontology import (
    MAX_DICTIONARY_CONTEXT_BYTES,
    apply_quote_repairs,
    dictionary_context,
    dictionary_fact,
    extraction_prompts,
    extraction_quote_issues,
    quote_label,
)
from .progress import analysis_progress
from .store import Conflict
from .transports import reasoning_budget, validate_job_tokens

_UNSET_TOKENS = object()


class NothingToPublish(ValueError):
    """Exclusions left a complete extraction unpublishable: no candidates, no published claims, revision not consumed."""


def text(value, name, limit=200, *, allow_cr=False):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 and c not in ('\n\t\r' if allow_cr else '\n\t') for c in value):
        raise ValueError(f'{name}: 비어 있지 않은 {limit}자 이하 문자열이 필요합니다.')
    return value.strip()


def quote_text(value):
    """Source quotation shape, not authored prose; grounding is checked separately."""
    if (not isinstance(value, str) or len(value) > 8000 or '\x00' in value
            or not any(ord(c) >= 32 and not c.isspace() for c in value)):
        raise ValueError('quote: NUL 없이 본문이 포함된 8000자 이하 문자열이 필요합니다.')
    # Ingestion admits non-NUL controls (including PDF extraction artifacts).
    # Retain interior characters and the established boundary normalization.
    return value.strip()


def evidence(value, source):
    quote = quote_text(value)
    if quote not in source:
        raise ValueError('원문에 없는 인용입니다.')
    return quote


def validate_fact(value, source):
    if not isinstance(value, dict):
        raise ValueError('사실은 JSON 객체여야 합니다.')
    return {k: text(value.get(k), k) for k in ('subject', 'predicate', 'object')} | {'quote': evidence(value.get('quote'), source)}


def validate_page(value, source, *, allow_no_claims=False):
    if not isinstance(value, dict) or not isinstance(value.get('claims'), list) or not (0 if allow_no_claims else 1) <= len(value['claims']) <= 192:
        raise ValueError('위키 claims는 1~192개여야 합니다.')
    claims = []
    for c in value['claims']:
        if not isinstance(c, dict):
            raise ValueError('위키 주장은 객체여야 합니다.')
        claims.append({'heading': text(c.get('heading'), 'heading'), 'text': text(c.get('text'), 'text', 1500), 'quote': evidence(c.get('quote'), source)})
    return {'title': text(value.get('title'), 'title'), 'claims': claims}


def error_text(exc):
    """str(exc), or the exception's type name when its own __str__ raises."""
    try:
        return str(exc)
    except Exception:
        return type(exc).__name__


class Service:
    def __init__(self, store, root, cli=None, *, credentials=None, relation_type_warning_threshold=20, extraction_caller=None, extraction_caller_factory=None, quote_repair_limit=None):
        if extraction_caller_factory is not None and (extraction_caller is not None or not callable(extraction_caller_factory)):
            raise ValueError('추출 호출자 공급 계약 오류')
        from .openrouter import OpenRouter
        from .transports import Credentials
        configured = Credentials.from_server(credentials) if credentials is not None and credentials.openrouter_key() else None
        from .dictionary_type_warnings import validate_threshold
        self.relation_type_warning_threshold = validate_threshold(relation_type_warning_threshold)
        self.store, self.root, self.cli = store, Path(root).resolve(), cli or CLI(
            rates=store.usage_rates, http_adapter=OpenRouter(), credentials=configured)
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='verinote-extract')
        self.slots = threading.BoundedSemaphore(8)
        self.extraction_lock = threading.RLock()
        # None is the product: whitespace-variant location, then one repair per chunk, row-level exclusion. An integer is a
        # benchmark's pre-registered contract: stop before the next repair, all-or-nothing, so no location or exclusion happens.
        self.quote_repair_limit = quote_repair_limit
        self.extraction_caller = extraction_caller
        self.extraction_caller_factory = extraction_caller_factory
        self.extraction_owner = None
        self.closed = False
        self.draining = False
        self.close_lock = threading.Lock()
        self.registered_extractions = {}
        self.cleanup_lock = threading.Lock()
        self.cleanup_logs()

    def extraction_transport(self, model, *, capacity=2, spec=None):
        """Public factory rejects admission once graceful shutdown begins."""
        from .extraction_transport import ExtractionSpec
        with self.extraction_lock:
            if self.closed: raise ValueError('Service가 닫혔습니다.')
            return self._extraction_transport(spec or ExtractionSpec.capture(self.cli, model, capacity))

    def _registered_extraction_transport(self, run, spec):
        with self.extraction_lock:
            if (run['id'] not in self.registered_extractions
                    or self.registered_extractions[run['id']] != run['extraction_options']
                    or (self.closed and not self.draining)):
                raise ValueError('등록된 추출 실행이 아닙니다.')
            return self._extraction_transport(spec)

    def _extraction_transport(self, spec):
        from .extraction_transport import ExtractionTransport
        if self.extraction_owner is not None:
            if self.extraction_owner.identity == spec and not self.extraction_owner.closed:
                return self.extraction_owner.view()
            self.extraction_owner.retire_if_idle()
            self.extraction_owner = None
        self.extraction_owner = ExtractionTransport(spec)
        return self.extraction_owner.view()

    def close(self):
        """One closer drains all registered work before attempting owner cleanup."""
        with self.close_lock:
            with self.extraction_lock:
                self.closed = self.draining = True
            # If waiting is interrupted, retain owners and drain permission for a retry.
            self.pool.shutdown(wait=True)
            with self.extraction_lock:
                self.draining = False
                try:
                    if self.extraction_owner is not None: self.extraction_owner.close()
                finally:
                    if hasattr(self.cli, 'close'): self.cli.close()

    def with_usage(self, kind, owner, *, coverage_details=True):
        from .usage import job_calls, summarize
        calls = job_calls(self.root, kind, owner)
        result = owner | {'usage': summarize(calls) | {'details': calls}}
        report = None
        if kind == 'runs' and owner.get('stage') == 'extract':
            report = read_report(self.root, self.store, owner['id'])
            result['coverage'] = report
        if kind == 'runs' and owner.get('stage') == 'ontology':
            result['dictionary_exclusions'] = self.dictionary_exclusions(owner['id'], include_items=coverage_details)
            result['dictionary_reconciliations'] = self.dictionary_reconciliations(owner['id'], include_items=coverage_details)
            from .dictionary_type_warnings import read_run_warnings
            result['relation_type_warnings'] = read_run_warnings(self.root, self.store, owner['id'], include_items=coverage_details)
        if kind == 'runs':
            result['progress'] = analysis_progress(self.root, self.store, owner['id'], report)
        if report is not None and not coverage_details:
            report.pop('chunks', None)
        return result

    def dictionary_exclusions(self, run_id, *, document_id=None, version=None, include_items=True):
        from .dictionary_exclusions import read_report
        return read_report(self.root, self.store, run_id, document_id=document_id, version=version, include_items=include_items)

    def dictionary_reconciliations(self, run_id, *, document_id=None, version=None, include_items=True):
        from .dictionary_reconciliation import read_report
        return read_report(self.root, self.store, run_id, document_id=document_id, version=version, include_items=include_items)

    def usage_report(self):
        from .usage import job_calls, summarize
        owners, total = self.store.usage_owners()
        calls: list[object] = []
        groups: dict[tuple, list[object]] = {}
        for owner in owners:
            for call in job_calls(self.root, owner['kind'], owner):
                calls.append(call)
                key = (owner['agent'], call.get('resolved_model') or owner['model'] or '', call['stage'])
                groups.setdefault(key, []).append(call)
        return {'scope': 'retained_jobs', 'owner_limit': 1000, 'included_jobs': len(owners),
                'total_jobs': total, 'truncated': total > len(owners), 'summary': summarize(calls),
                'groups': [{'agent': key[0], 'model': key[1], 'stage': key[2], 'usage': summarize(group)} for key, group in sorted(groups.items())]}

    def register_transport(self, agent, model, question=None, *, stage=None, max_tokens=_UNSET_TOKENS, answer_reserve_tokens=_UNSET_TOKENS):
        if agent == 'ollama':
            from .ollama import Job
            snapshot = None
            if stage == 'extract' and getattr(self.cli, 'ollama_fidelity', False):
                from .ollama_fidelity import candidate_options
                snapshot = candidate_options()
            caller = Job(self.cli, snapshot, model=model)
            return caller.options, caller
        if agent != 'openrouter':
            return None, copy.copy(self.cli) if isinstance(self.cli, CLI) else self.cli
        from .job_transport import JobTransport, capture_options
        from .openrouter import _contains_secret
        if not isinstance(self.cli, CLI) or not self.cli.ready(agent):
            raise ValueError('OpenRouter HTTP 어댑터 또는 API 자격 증명이 구성되지 않았습니다.')
        options = capture_options(self.cli)
        if max_tokens is not _UNSET_TOKENS:
            options['max_tokens'] = validate_job_tokens(max_tokens)
        if answer_reserve_tokens is not _UNSET_TOKENS:
            options.update(adapter_contract_version=2, answer_reserve_tokens=answer_reserve_tokens,
                           reasoning_max_tokens=reasoning_budget(options['max_tokens'], answer_reserve_tokens))
        if stage == 'extract':
            options['timeouts']['default'] = self.cli.limited_timeout(agent, CHUNK_TIMEOUT).effective_timeout(agent)
        key = self.cli._credentials.api_key
        if any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise ValueError('OpenRouter 자격 증명 형식이 올바르지 않습니다.')
        rates = copy.deepcopy(self.cli.rates())
        if _contains_secret([model, question, options, rates], key):
            raise ValueError('OpenRouter 작업 입력에 자격 증명을 포함할 수 없습니다.')
        return options, JobTransport(self.cli, options, rates=rates)

    def other_agent_ready(self, agent):
        """Whether cli.ready reports a provider other than this agent and Ollama; a missing or failing check counts as no."""
        ready = getattr(self.cli, 'ready', None)
        if not callable(ready):
            return False
        for other in PROVIDERS:
            if other not in (agent, 'ollama'):
                try:
                    if ready(other):
                        return True
                except Exception:
                    continue
        return False

    def job_cli(self, job):
        if job['agent'] == 'ollama':
            from .ollama import Job
            return Job(self.cli, job.get('execution_options'))
        if job['agent'] != 'openrouter':
            return self.cli
        from .job_transport import JobTransport
        if not isinstance(self.cli, CLI) or not self.cli.ready('openrouter'):
            raise ValueError('OpenRouter 자격 증명이 구성되지 않았습니다.')
        return JobTransport(self.cli, job.get('execution_options'))

    def start(self, document_id, agent, model, stage='extract', expected_version=None, expected_generation=None, *, max_tokens=_UNSET_TOKENS, answer_reserve_tokens=_UNSET_TOKENS, regenerate=None, extraction_concurrency=None, extraction_policy=None):
        if agent not in PROVIDERS:
            raise ValueError('지원하지 않는 에이전트')
        model = validate_model(model, agent)
        with self.extraction_lock:
            if self.closed: raise ValueError('Service가 닫혔습니다.')
            if max_tokens is not _UNSET_TOKENS:
                if agent != 'openrouter':
                    raise ValueError('토큰 한도 설정은 OpenRouter 사전 생성·추출에서만 지원합니다.')
                validate_job_tokens(max_tokens)
            if answer_reserve_tokens is not _UNSET_TOKENS and agent != 'openrouter':
                raise ValueError('답변 여유 설정은 OpenRouter 사전 생성·추출에서만 지원합니다.')
            experiment = None
            if extraction_concurrency is not None and extraction_policy is not None:
                raise ValueError('추출 동시성과 정책은 함께 지정할 수 없습니다.')
            if extraction_concurrency is not None or extraction_policy is not None:
                from .extraction_options import experiment_request, policy_request
                from .extraction_transport import ExtractionSpec
                if agent != 'claude' or stage != 'extract':
                    raise ValueError('병렬 추출은 Claude 추출에서만 지원합니다.')
                if extraction_policy is None and (type(extraction_concurrency) is not int or extraction_concurrency not in (1, 2)):
                    raise ValueError('추출 worker 수는 1 또는 2여야 합니다.')
                spec = (self.extraction_caller.extraction_spec if self.extraction_caller is not None
                        else ExtractionSpec.capture(self.cli, model, extraction_concurrency if extraction_policy is None else 1))
                if spec.model != model or (extraction_policy is None and spec.capacity != extraction_concurrency):
                    raise ValueError('추출 호출자 계약 불일치')
                experiment = experiment_request(spec) if extraction_policy is None else policy_request(spec, extraction_policy)
            context_limit = validate_context_bytes(self.store.settings().get('dictionary_context_bytes', MAX_DICTIONARY_CONTEXT_BYTES))
            options, transport = self.register_transport(agent, model, stage=stage, max_tokens=max_tokens, answer_reserve_tokens=answer_reserve_tokens)
            if not self.slots.acquire(blocking=False):
                raise ValueError('작업 대기열이 가득 찼습니다.')
            from .extraction_timing import create_timing
            timing = create_timing(self.root, agent, stage, schema_version=2 if experiment else 1)
            run = failed = None
            try:
                run = self.store.create_run(document_id, agent, model, stage, expected_version, expected_generation, **({'regenerate': regenerate} if regenerate is not None else {}), **({'execution_options': options} if options is not None else {}), **({'extraction_options': experiment} if experiment is not None else {}))
                timing.bind(run)
                from .usage import atomic_json
                policy_folder = self.root / 'runs' / run['id']
                policy_folder.mkdir(parents=True, exist_ok=True)
                atomic_json(policy_folder/'context-policy.json', {'schema_version': 1, 'run_id': run['id'], 'dictionary_context_bytes': context_limit})
                if experiment is not None: self.registered_extractions[run['id']] = copy.deepcopy(run['extraction_options'])
                self.pool.submit(self.extract, copy.deepcopy(run), transport, self.relation_type_warning_threshold, timing, context_limit)
                return run
            except Exception as exc:
                failed = exc
                if run is not None: self.registered_extractions.pop(run['id'], None)
        try:  # outside extraction_lock: the status writes can each wait for a locked database, and a start of another document must not wait on them
            if run is not None: self.record_failure(run['id'], error_text(failed))
        finally:
            self.slots.release()
        raise failed

    def extract(self, run, transport=None, relation_type_warning_threshold=None, timing=None, context_limit=MAX_DICTIONARY_CONTEXT_BYTES):
        from .extraction_timing import create_timing, error_kind
        if timing is None:
            timing = create_timing(self.root, run['agent'], run['stage'], submitted=False, schema_version=2 if run.get('extraction_options') is not None else 1)
            timing.bind(run)
        report = None
        report_ok = True
        checkpoint = None
        active_chunk = None
        coverage_repairs = 0
        located_quotes = 0
        located_places: list[str] = []
        published = failing = False
        scheduler = None
        try:
            timing.worker_start()
            experiment = run.get('extraction_options')
            if experiment is not None:
                from .extraction_options import (
                    extraction_options,
                    validate_extraction_source,
                )
                from .extraction_transport import ExtractionSpec
                experiment = extraction_options(experiment, agent=run['agent'], stage=run['stage'], model=run['model'])
                validate_extraction_source(experiment, self.store.document(run['document_id'], run['version'])['text'])
                spec = ExtractionSpec.from_dict(experiment['transport'])
                if self.extraction_caller_factory is not None or self.extraction_caller is not None:
                    cli = self.extraction_caller_factory(spec) if self.extraction_caller_factory is not None else self.extraction_caller
                    if cli.extraction_spec != spec: raise ValueError('추출 호출자 계약 불일치')
                else:
                    cli = self._registered_extraction_transport(run, spec)
            else:
                cli = transport if transport is not None else self.job_cli(run)
            context_limit = validate_context_bytes(context_limit)
            self.store.run_status(run['id'], 'running')
            doc = self.store.document(run['document_id'], run['version'])
            if run['stage'] == 'ontology':
                from .dictionary_pipeline import generate_dictionary
                dictionary, detail = generate_dictionary(self.store, cli, self.root, run, doc, relation_type_warning_threshold=self.relation_type_warning_threshold if relation_type_warning_threshold is None else relation_type_warning_threshold, context_limit=context_limit, other_agent_ready=lambda: self.other_agent_ready(run['agent']))
                self.store.finish_run(run['id'], dictionary=dictionary, detail=detail)
                return
            timing.configure(doc)
            contract_version = run['ontology'].get('extraction_contract', 0)
            extraction_prompt, quote_repair_prompt = extraction_prompts(contract_version)
            current_contract = contract_version in (1, 2)
            with timing.section('restore'):
                if current_contract:
                    report = new_report(run, len(doc['text']))
                    checkpoint = ExtractionCheckpoint(self.store, self.root, run, doc)
                claims: list[object] = []
                facts: list[object] = []
                repair_calls, repaired_quotes, repair_locations = 0, 0, []
                if checkpoint is not None:
                    assert report is not None
                    for index, output in enumerate(checkpoint.outputs):
                        offset, chunk = checkpoint.slices[index]
                        claims.extend(output['claims'])
                        facts.extend(output['facts'])
                        report['chunks'].append(chunk_report(output, offset, len(chunk)))
                    if checkpoint.outputs:
                        report_ok = write_report(self.root, report)
            timing.reuse(checkpoint.resumed_chunks if checkpoint is not None else 0,
                         checkpoint.state['resumed_from'] if checkpoint is not None else None)
            from .extraction_scheduler import ChunkFailure, ChunkScheduler, ChunkTask

            pinned = {'approved_dictionary': run['ontology'] | {'payload': dictionary_context(run['ontology']['payload'])}}

            def chunk_payload(offset, chunk):
                return {'filename': doc['filename'], 'version': doc['version'], 'offset': offset, 'text': chunk}

            def prepare(raw):
                index, (offset, chunk) = raw
                payload = chunk_payload(offset, chunk)
                folder = self.root / 'runs' / run['id'] / str(index)
                prompt = prepare_dictionary_prompt(cli, run['agent'], extraction_prompt, payload | pinned, context_limit, folder)
                return ChunkTask(index, offset, chunk, payload, prompt, folder, time.monotonic() + CHUNK_TIMEOUT)

            def prepare_next(raw):
                try:
                    return prepare(raw)
                except Exception as exc:
                    raise ChunkFailure(raw[0], exc) from exc

            def call_task(task, prompt, target):
                remaining = task.deadline - time.monotonic()
                if remaining <= 0: raise TimeoutError(f'청크 실행 시간 제한 초과 ({CHUNK_TIMEOUT}초)')
                limited = cli.limited_timeout(run['agent'], remaining) if hasattr(cli, 'limited_timeout') else cli
                with timing.call(task.index, target != task.folder):
                    return limited.complete(run['agent'], run['model'], prompt, target)

            def begin_call():
                if checkpoint is not None: checkpoint.begin_call()

            remaining_chunks = [(i, pair) for i, pair in enumerate(chunks(doc['text']), 1)
                                if checkpoint is None or i > checkpoint.resumed_chunks]
            # The pin and the budget are fixed for this run, so a chunk refused here would be refused when reached:
            # refuse before paying for the earlier ones, reporting the chunk with the largest excess.
            admissions = [(raw, refusal(cli, run['agent'], extraction_prompt, chunk_payload(*raw[1]) | pinned, context_limit)) for raw in remaining_chunks]
            oversized = [(raw, too_large) for raw, too_large in admissions if too_large is not None]
            if oversized:
                worst = max(oversized, key=lambda pair: pair[1].excess)[0]
                active_chunk = worst[0]
                try:
                    prepare(worst)  # records the unfit context-budget.json
                except RequestTooLarge as actual:
                    raise actual.with_advice(actual.extraction_advice(self.other_agent_ready(run['agent']), run['ontology']['revision'])) from actual
                raise AssertionError('추출 사전 검사와 요청 준비가 일치하지 않습니다.')
            iterator: Iterator[ChunkTask]
            if experiment is not None:
                scheduler = ChunkScheduler(remaining_chunks, experiment['concurrency'], prepare_next, call_task, begin_call, timing)
                iterator = scheduler
            else:
                iterator = map(prepare_next, remaining_chunks)
            with scheduler if scheduler is not None else nullcontext():
                for task in iterator:
                    index, offset, chunk = task.index, task.offset, task.text
                    payload, folder, deadline = task.payload, task.folder, task.deadline
                    active_chunk = index
                    if scheduler is None: timing.begin_chunk(index)
                    self.store.run_status(run['id'], 'running', f'승인 사전으로 추출 · 청크 {index}/{(len(doc["text"]) + 7999) // 8000} · 청크 제한 {CHUNK_TIMEOUT}초'
                                          + (f' · {checkpoint.summary()}' if checkpoint is not None else '')
                                          + (f' · {experiment["policy"]} · 전체 {experiment["original_chunk_count"]}청크 · 동시 {experiment["concurrency"]}' if experiment and experiment['schema_version']==2 else ''))

                    def complete_chunk(prompt, target):
                        if scheduler is not None: return scheduler.repair(task, prompt, target)  # noqa: B023
                        begin_call()
                        return call_task(task, prompt, target)  # noqa: B023

                    output = task.output if scheduler is not None else complete_chunk(task.prompt, folder)
                    with caller_validation(cli, folder, run['agent']) as validation, timing.section('validation_with_bookkeeping', index):
                        if time.monotonic() > deadline:
                            raise TimeoutError(f'청크 실행 시간 제한 초과 ({CHUNK_TIMEOUT}초)')
                        excluded: list[dict] = []
                        try:
                            raw_output = output
                            if current_contract:
                                output, excluded = exclude_off_dictionary_facts(output, run['ontology']['payload'])
                                if excluded:
                                    validation.reject()
                            issues = extraction_quote_issues(output, chunk, run['ontology']['payload'], contract_version=contract_version, skip_off_dictionary=current_contract)
                            if current_contract:
                                normalized, accounting_changes = repair_missing_indices(output, run['ontology']['payload'])
                                if accounting_changes:
                                    validation.reject()
                                    write_repair_audit(self.root, run, doc, index, offset, raw_output, normalized, accounting_changes)
                                    coverage_repairs += len(accounting_changes)
                                output = normalized
                            if issues:
                                validation.reject()
                            located: list[dict] = []
                            refused: list[dict] = []
                            if issues and contract_version == 2 and self.quote_repair_limit is None:
                                output, located, issues = locate_whitespace_variants(output, chunk, issues)
                                if located:
                                    write_located_quotes(folder, located)
                            if issues:
                                locations = ', '.join(quote_label(issue['path']) for issue in issues[:5])
                                if len(issues) > 5:
                                    locations += f' 외 {len(issues) - 5}개'
                                if self.quote_repair_limit is not None and repair_calls >= self.quote_repair_limit:
                                    raise ValueError(f'{locations}: 원문에 없는 인용 또는 위치가 모호한 인용입니다. 벤치마크 인용 보정 {self.quote_repair_limit}회 한도에 도달했습니다.')
                                self.store.run_status(run['id'], 'running', f'청크 {index} · {locations}: 원문 인용 보정 중 (이번 실행 추가 호출 {repair_calls + 1}회째 · 청크당 1회)')
                                try:
                                    with timing.section('repair', index):
                                        repair_folder = folder / 'quote-repair'
                                        repair_prompt = prepare_dictionary_prompt(cli, run['agent'], quote_repair_prompt,
                                            payload | {'issues': issues, 'approved_dictionary': run['ontology']}, context_limit, repair_folder)
                                        repair_calls += 1
                                        response = complete_chunk(repair_prompt, repair_folder)
                                        with caller_validation(cli, repair_folder, run['agent']) as repair_validation, timing.section('repair_validation_with_bookkeeping', index):
                                            if time.monotonic() > deadline:
                                                raise TimeoutError(f'청크 실행 시간 제한 초과 ({CHUNK_TIMEOUT}초)')
                                            if current_contract and self.quote_repair_limit is None:
                                                output, changes, refused = resolve_quote_repairs(output, chunk, issues, response, contract_version)
                                            else:
                                                output, changes = apply_quote_repairs(output, chunk, issues, response)
                                            if refused:
                                                repair_validation.reject()
                                            no_claims = bool(output.get('excluded_claims'))
                                            # Recheck the whole response; a repair cannot bypass any vocabulary/shape gate.
                                            if extraction_quote_issues(output, chunk, run['ontology']['payload'], require_coverage=current_contract, contract_version=contract_version, allow_no_claims=no_claims):
                                                raise ValueError('보정 후에도 원문에 없거나 위치가 모호한 인용이 있습니다.')
                                            validate_page(output, chunk, allow_no_claims=no_claims)
                                            for fact in output['facts']:
                                                dictionary_fact(fact, chunk, run['ontology']['payload'])
                                            repair_folder.mkdir(parents=True, exist_ok=True)
                                            (repair_folder / 'changes.json').write_text(json.dumps(changes, ensure_ascii=False, indent=2), encoding='utf-8')
                                            if refused:
                                                (repair_folder / 'refused.json').write_text(json.dumps(refused, ensure_ascii=False, indent=2), encoding='utf-8')
                                except RequestTooLarge as exc:
                                    raise ValueError(f'{locations}: 인용 보정 실패 (청크당 1회). {exc.with_advice(exc.repair_advice(self.other_agent_ready(run["agent"]), run["ontology"]["revision"]))}') from exc
                                except Exception as exc:
                                    raise ValueError(f'{locations}: 인용 보정 실패 (청크당 1회). {exc}') from exc
                                repaired_quotes += len(changes)
                                if changes:
                                    saved = [quote_label(path) for path in saved_quote_paths(changes, refused)]
                                    repair_locations.append(f'청크 {index}: ' + ', '.join(saved[:5]) + (f' 외 {len(saved) - 5}개' if len(saved) > 5 else ''))
                            page = validate_page(output, chunk, allow_no_claims=bool(output.get('excluded_claims')))
                        except ValueError as exc:
                            basis = ' (관계 번호는 사전 불일치 제외 뒤 기준)' if excluded and re.search(r'관계 \d+ 인용', str(exc)) else ''
                            raise ValueError(f'{exc}{basis}') from exc
                        claims.extend(page['claims'])
                        facts.extend(dictionary_fact(f, chunk, run['ontology']['payload']) for f in output['facts'])
                        if time.monotonic() > deadline:
                            raise TimeoutError(f'청크 실행 시간 제한 초과 ({CHUNK_TIMEOUT}초)')
                        with scheduler.accepting(task) if scheduler is not None else nullcontext():
                            if report is not None:
                                report['chunks'].append(chunk_report(output, offset, len(chunk)))
                                report_ok = write_report(self.root, report)
                            if checkpoint is not None:
                                checkpoint.accept(output, chunk, before_accept=(lambda task=task: scheduler.check(task)) if scheduler is not None else None)
                        if located:  # Counted here, not at location: a repair or check that fails after it leaves nothing saved.
                            located_quotes += len(located)
                            saved = [quote_label(path) for path in saved_quote_paths(located, refused)]
                            located_places.append(f'청크 {index}: ' + ', '.join(saved[:5]) + (f' 외 {len(saved) - 5}개' if len(saved) > 5 else ''))
                    timing.end_chunk(index=index)
            active_chunk = None
            unique = {json.dumps(f, sort_keys=True, ensure_ascii=False): f for f in facts}
            if checkpoint is not None and (reason := checkpoint.nothing_to_publish(claims, unique)):
                raise NothingToPublish(reason)
            shown = '; '.join(repair_locations[:5]) + (f' 외 {len(repair_locations) - 5}개 청크' if len(repair_locations) > 5 else '')
            detail = (f'이번 실행 원문 인용 {repaired_quotes}개 보정 · 추가 호출 {repair_calls}회 ({shown} · 번호는 제외 뒤 기준).' if repaired_quotes
                      else f'이번 실행 인용 보정 추가 호출 {repair_calls}회: 반영된 보정 없음.' if repair_calls else '')
            if located_quotes:
                detail = f'{detail} {located_quote_notice(located_quotes, located_places)}.'.lstrip()
            if repair_calls or located_quotes or (checkpoint is not None and checkpoint.resumed_chunks):
                detail = f'{detail} 의미가 맞는지 원문과 대조한 뒤 승인하세요.'.lstrip()
            if report is not None:
                detail = f'관계 후보 {len(unique)}개 · 관계별 점검은 실행 기록에서 확인하세요. 새 후보는 별도 승인 전까지 그래프에 포함되지 않습니다. ' + detail
                if not report_ok:
                    detail += ' 관계별 추출 점검 기록 저장 실패: 보고서를 사용할 수 없습니다.'
            if checkpoint is not None:
                detail += ' ' + checkpoint.summary()
            if coverage_repairs:
                detail += f' · 이번 실행 추출 점검 인덱스 {coverage_repairs}건 로컬 보정 (보정용 모델 호출 없음)'
            if checkpoint is not None and checkpoint.exclusion_notice():
                detail = f'{checkpoint.exclusion_notice()} · {detail}'
            if checkpoint is not None and checkpoint.quote_exclusion_notice():
                detail = (f'{checkpoint.quote_exclusion_notice()} 이 사전 revision과 추출 방식으로는 다시 추출할 수 없고 제외 항목만 복구하는 기능은 없습니다. '
                          f'사전을 수정·재승인해 재추출하면 모든 청크를 새로 호출하며 같은 결과가 나올 수 있습니다. · {detail}')
            with timing.section('publish'):
                self.store.finish_run(run['id'], {'title': doc['title'], 'claims': claims}, list(unique.values()), detail=detail[:2000])
                published = True
            if checkpoint is not None: checkpoint.finish('complete')
            timing.outcome('success')
        except Exception as exc:
            if published:
                timing.outcome('success')
                return
            from .extraction_scheduler import ChunkFailure
            if isinstance(exc, ChunkFailure):
                active_chunk, exc = exc.index, exc.cause
            detail, failing = error_text(exc), True  # the finally below writes the error status even if this bookkeeping raises
            timing.end_chunk(error_kind(exc))
            timing.outcome('error', error_kind(exc))
            if report is not None:
                write_report(self.root, report)
            if active_chunk is not None:
                detail = f'청크 {active_chunk} · {detail}'
            if coverage_repairs:
                detail += f' · 이번 실행 추출 점검 인덱스 {coverage_repairs}건 로컬 보정 (보정용 모델 호출 없음)'
            if located_quotes:
                detail += f' · {located_quote_notice(located_quotes, located_places)}'
            if checkpoint is not None:
                checkpoint.finish('error')
                if isinstance(exc, NothingToPublish):
                    detail += f' · {checkpoint.summary()}'
                else:
                    if checkpoint.quote_exclusion_notice():
                        detail += f' · {checkpoint.quote_exclusion_notice()}'
                    if checkpoint.exclusion_notice():
                        detail += f' · {checkpoint.exclusion_notice()}'
                    detail += f' · {checkpoint.summary()}'
                    if not isinstance(exc, (RequestTooLarge, Conflict)):  # a refusal is refused again; a Conflict ended on a changed run, document or dictionary
                        detail += f' · {checkpoint.retry_notice()}'
        finally:
            if failing: self.record_failure(run['id'], detail)
            with self.extraction_lock:
                self.registered_extractions.pop(run['id'], None)
            self.slots.release()
            timing.finish()

    FAILURE_FALLBACK = '실행이 실패했지만 실패 상세를 저장하지 못했습니다.'

    def record_failure(self, run_id, detail):
        """Move a failed run to error. Only a restart (Store.recover) moves a run left queued or running: until then a new run of its
        document, a dictionary change for its version and the deletion of every document are refused. Tries the detail twice, then the
        detail with characters SQLite cannot store escaped, then a fixed sentence, and gives up silently after that."""
        escaped = detail.encode('utf-8', 'backslashreplace').decode('utf-8')
        texts = [detail[:2000], detail[:2000]] + ([escaped[:2000]] if escaped != detail else []) + [self.FAILURE_FALLBACK]
        for text in texts:
            try:
                self.store.run_status(run_id, 'error', text)
                return
            except Exception:
                continue

    def query(self, request):
        if not isinstance(request, dict) or set(request) - {'snapshot', 'action', 'subject', 'predicate', 'target'}:
            raise ValueError('알 수 없는 질의 필드')
        snapshot = self.store.snapshot(request.get('snapshot'))
        result = query(snapshot, **{k: v for k, v in request.items() if k != 'snapshot'})
        return result | {'query_id': self.store.save_query(result)}

    def ask(self, request):
        if not isinstance(request, dict) or set(request) - {'question', 'agent', 'model', 'snapshot'}:
            raise ValueError('알 수 없는 질문 필드')
        question = text(request.get('question'), '질문', 2000)
        config = self.store.settings()
        agent = request.get('agent', config['selected'])
        if not isinstance(agent, str) or agent not in PROVIDERS:
            raise ValueError('지원하지 않는 에이전트')
        model = validate_model(request.get('model', config['models'].get(agent, '')), agent)
        options, transport = self.register_transport(agent, model, question)
        snapshot_id = request.get('snapshot')
        if snapshot_id is not None and (not isinstance(snapshot_id, str) or len(snapshot_id) != 64):
            raise ValueError('스냅샷 ID 형식 오류')
        if not self.slots.acquire(blocking=False):
            raise ValueError('작업 대기열이 가득 찼습니다.')
        job = None
        try:
            snapshot = self.store.snapshot(snapshot_id)
            job = self.store.create_question(question, agent, model, snapshot['id'], **({'execution_options': options} if options is not None else {}))
            self.pool.submit(self.answer, copy.deepcopy(job), snapshot, transport)
            return job
        except Exception as exc:
            if job:
                job.update(status='error', error=str(exc))
                self.store.save_question(job)
            self.slots.release()
            raise

    def answer(self, job, snapshot, transport=None):
        from .assistant import Assistant
        def progress(trace, evidence):
            job.update(trace=trace, evidence=evidence)
            self.store.save_question(job)
        try:
            cli = transport if transport is not None else self.job_cli(job)
            job['status'] = 'running'
            self.store.save_question(job)
            assistant = Assistant(self.store, cli, snapshot, job, self.root / 'questions' / job['id'], progress)
            self.store.save_question(job)
            job['answer'] = assistant.run()
            job['status'] = 'success'
        except Exception as exc:
            job.update(status='error', error=str(exc)[:2000])
        finally:
            try:
                self.store.save_question(job)
            finally:
                self.slots.release()

    def cleanup_logs(self):
        # Obligations are persisted before DB rows disappear. Never follow a log-root symlink.
        with self.cleanup_lock:
            for task in self.store.pending_cleanup():
                kind, key = task['kind'], task['id']
                if kind not in ('runs', 'questions') or not re.fullmatch(r'[a-f0-9]{32}', key):
                    continue
                root = self.root.resolve()
                parent = root / kind
                path = parent / key
                try:
                    if parent.is_symlink() or path.is_symlink():
                        continue
                    if path.exists():
                        shutil.rmtree(path)
                    self.store.finish_cleanup(kind, key)
                except OSError:
                    # Keep the durable obligation; startup and explicit retry will try again.
                    continue
            pending = len(self.store.pending_cleanup())
            return {'cleanup_pending': pending, 'cleanup_status': 'pending' if pending else 'complete'}

    def delete_document(self, document_id, latest_only, request):
        if not isinstance(request, dict) or set(request) != {'expected_version', 'expected_generation'}:
            raise ValueError('expected_version과 expected_generation만 필요합니다.')
        result = self.store.delete_document(document_id, latest_only, request['expected_version'], request['expected_generation'])
        return result | self.cleanup_logs()
