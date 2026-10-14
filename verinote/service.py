"""Bounded extraction orchestration; validation never performs human approval."""
from concurrent.futures import ThreadPoolExecutor
import json
import re
import shutil
from pathlib import Path
import threading

from .agents import CLI, PROVIDERS, validate_model
from .documents import chunks
from .engine import query
from .ontology import (DICTIONARY_PROMPT, EXTRACTION_PROMPT, MAX_FACTS,
                       bounded_prompt, dictionary_fact, merge_dictionary)


def text(value, name, limit=200):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(c) < 32 and c not in '\n\t' for c in value):
        raise ValueError(f'{name}: 비어 있지 않은 {limit}자 이하 문자열이 필요합니다.')
    return value.strip()


def evidence(value, source):
    quote = text(value, 'quote', 8000)
    if quote not in source:
        raise ValueError('원문에 없는 인용입니다.')
    return quote


def validate_fact(value, source):
    if not isinstance(value, dict):
        raise ValueError('사실은 JSON 객체여야 합니다.')
    return {k: text(value.get(k), k) for k in ('subject', 'predicate', 'object')} | {'quote': evidence(value.get('quote'), source)}


def validate_page(value, source):
    if not isinstance(value, dict) or not isinstance(value.get('claims'), list) or not 1 <= len(value['claims']) <= 192:
        raise ValueError('위키 claims는 1~192개여야 합니다.')
    claims = []
    for c in value['claims']:
        if not isinstance(c, dict):
            raise ValueError('위키 주장은 객체여야 합니다.')
        claims.append({'heading': text(c.get('heading'), 'heading'), 'text': text(c.get('text'), 'text', 1500), 'quote': evidence(c.get('quote'), source)})
    return {'title': text(value.get('title'), 'title'), 'claims': claims}


class Service:
    def __init__(self, store, root, cli=None):
        self.store, self.root, self.cli = store, Path(root), cli or CLI()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='verinote-extract')
        self.slots = threading.BoundedSemaphore(8)
        self.cleanup_lock = threading.Lock()
        self.cleanup_logs()

    def start(self, document_id, agent, model, stage='extract', expected_version=None, expected_generation=None):
        if agent not in PROVIDERS:
            raise ValueError('지원하지 않는 에이전트')
        model = validate_model(model)
        if not self.slots.acquire(blocking=False):
            raise ValueError('작업 대기열이 가득 찼습니다.')
        try:
            run = self.store.create_run(document_id, agent, model, stage, expected_version, expected_generation)
            self.pool.submit(self.extract, run)
            return run
        except Exception:
            self.slots.release()
            raise

    def extract(self, run):
        try:
            self.store.run_status(run['id'], 'running')
            doc = self.store.document(run['document_id'], run['version'])
            claims, facts = [], []
            dictionary = {'entities': [], 'aliases': [], 'relations': []}
            for index, (offset, chunk) in enumerate(chunks(doc['text']), 1):
                label = '사전 생성' if run['stage'] == 'ontology' else '승인 사전으로 추출'
                self.store.run_status(run['id'], 'running', f'{label} · 청크 {index}/{(len(doc["text"]) + 7999) // 8000}')
                payload = {'filename': doc['filename'], 'version': doc['version'], 'offset': offset, 'text': chunk}
                if run['stage'] == 'ontology':
                    prompt = bounded_prompt(DICTIONARY_PROMPT, payload | {'dictionary': dictionary})
                else:
                    prompt = bounded_prompt(EXTRACTION_PROMPT, payload | {'approved_dictionary': run['ontology']})
                output = self.cli.complete(run['agent'], run['model'], prompt, self.root / 'runs' / run['id'] / str(index))
                if run['stage'] == 'ontology':
                    dictionary = merge_dictionary(dictionary, output, doc['text'][:offset + len(chunk)], chunk)
                    continue
                page = validate_page(output, chunk)
                if len(page['claims']) > 12 or not isinstance(output.get('facts'), list) or len(output['facts']) > MAX_FACTS:
                    raise ValueError(f'청크당 주장 12개/관계 {MAX_FACTS}개 제한 초과')
                claims.extend(page['claims'])
                facts.extend(dictionary_fact(f, chunk, run['ontology']['payload']) for f in output['facts'])
            if run['stage'] == 'ontology':
                self.store.finish_run(run['id'], dictionary=dictionary)
                return
            unique = {json.dumps(f, sort_keys=True, ensure_ascii=False): f for f in facts}
            self.store.finish_run(run['id'], {'title': doc['title'], 'claims': claims}, list(unique.values()))
        except Exception as exc:
            self.store.run_status(run['id'], 'error', str(exc)[:2000])
        finally:
            self.slots.release()

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
        model = validate_model(request.get('model', config['models'].get(agent, '')))
        snapshot_id = request.get('snapshot')
        if snapshot_id is not None and (not isinstance(snapshot_id, str) or len(snapshot_id) != 64):
            raise ValueError('스냅샷 ID 형식 오류')
        if not self.slots.acquire(blocking=False):
            raise ValueError('작업 대기열이 가득 찼습니다.')
        job = None
        try:
            snapshot = self.store.snapshot(snapshot_id)
            job = self.store.create_question(question, agent, model, snapshot['id'])
            self.pool.submit(self.answer, dict(job), snapshot)
            return job
        except Exception as exc:
            if job:
                job.update(status='error', error=str(exc))
                self.store.save_question(job)
            self.slots.release()
            raise

    def answer(self, job, snapshot):
        from .assistant import Assistant
        def progress(trace, evidence):
            job.update(trace=trace, evidence=evidence)
            self.store.save_question(job)
        try:
            job['status'] = 'running'
            self.store.save_question(job)
            assistant = Assistant(self.store, self.cli, snapshot, job, self.root / 'questions' / job['id'], progress)
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
