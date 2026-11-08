"""Transactional immutable document versions and optimistic human review."""
import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .ontology import EXTRACTION_CONTRACT_VERSION, extraction_key


def now():
    return datetime.now(timezone.utc).isoformat()


def ident():
    return uuid.uuid4().hex


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value).encode()).hexdigest()


MAX_BULK_APPROVAL = 2000


class Conflict(ValueError):
    pass


class RunCancelled(Exception):
    """The run's status is cancelling or cancelled: its worker admits no further call. `summary` carries the stage's progress text when known."""
    summary = ''


QUEUED_CANCEL_DETAIL = '대기 중에 취소되었습니다. 이 실행은 모델을 호출하지 않았습니다.'


def cancel_result(stage, topic=True):
    """What a cancelled run does not save, by stage, with the topic particle (은/는) or the object particle (을/를)."""
    noun, vowel = ('사전 초안', False) if stage == 'ontology' else ('위키·관계 후보', True) if stage == 'extract' else ('결과', True)
    return noun + (('는' if vowel else '은') if topic else ('를' if vowel else '을'))


SCHEMA = '''
CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY,title TEXT NOT NULL,current_version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS versions(document_id TEXT,version INTEGER,filename TEXT,text TEXT,raw BLOB,sha256 TEXT,created_at TEXT,PRIMARY KEY(document_id,version));
CREATE TABLE IF NOT EXISTS items(id TEXT PRIMARY KEY,document_id TEXT,version INTEGER,kind TEXT,payload TEXT,status TEXT,revision INTEGER,run_id TEXT);
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,item_id TEXT,revision INTEGER,action TEXT,actor TEXT,reason TEXT,payload TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,document_id TEXT,version INTEGER,agent TEXT,model TEXT,status TEXT,detail TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS log_cleanup(kind TEXT, id TEXT, PRIMARY KEY(kind,id));
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE IF NOT EXISTS snapshots(id TEXT PRIMARY KEY,payload TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS questions(id TEXT PRIMARY KEY,payload TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS queries(id TEXT PRIMARY KEY,payload TEXT,created_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS active_extraction ON runs(document_id) WHERE status IN ('queued','running');
'''


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
        with self.connect(True) as db:
            columns = {r['name'] for r in db.execute('PRAGMA table_info(documents)')}
            if 'next_version' not in columns:
                db.execute('ALTER TABLE documents ADD COLUMN next_version INTEGER NOT NULL DEFAULT 2')
                db.execute('UPDATE documents SET next_version=(SELECT COALESCE(MAX(version),0)+1 FROM versions WHERE document_id=documents.id)')
            if 'generation' not in columns:
                db.execute('ALTER TABLE documents ADD COLUMN generation INTEGER NOT NULL DEFAULT 1')
            run_columns = {r['name'] for r in db.execute('PRAGMA table_info(runs)')}
            if 'stage' not in run_columns:
                db.execute("ALTER TABLE runs ADD COLUMN stage TEXT NOT NULL DEFAULT 'legacy'")
            if 'ontology' not in run_columns:
                db.execute('ALTER TABLE runs ADD COLUMN ontology TEXT')
            if 'execution_options' not in run_columns:
                db.execute('ALTER TABLE runs ADD COLUMN execution_options TEXT')
            if 'extraction_options' not in run_columns:
                db.execute('ALTER TABLE runs ADD COLUMN extraction_options TEXT')

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def recover(self):
        with self.connect(True) as db:
            db.execute("UPDATE runs SET status='interrupted',detail='서버가 재시작되었습니다. 다시 추출하세요.' WHERE status IN ('queued','running')")
            for row in db.execute("SELECT id,stage FROM runs WHERE status='cancelling'").fetchall():
                db.execute("UPDATE runs SET status='cancelled',detail=? WHERE id=?", (f"취소를 요청한 뒤 실행이 멈춘 것을 기록하기 전에 서버가 재시작되었습니다. {cancel_result(row['stage'])} 저장하지 않았습니다.", row['id']))

        with self.connect(True) as db:
            for row in db.execute('SELECT id,payload FROM questions').fetchall():
                job = json.loads(row['payload'])
                if job['status'] in ('queued', 'running'):
                    job.update(status='interrupted', error='서버 재시작으로 중단되었습니다.')
                    db.execute('UPDATE questions SET payload=? WHERE id=?', (canonical(job), row['id']))

    def create_question(self, question, agent, model, snapshot, *, execution_options=None):
        job = {'id': ident(), 'question': question, 'agent': agent, 'model': model,
               'snapshot': snapshot, 'status': 'queued', 'trace': [], 'evidence': {},
               'answer': None, 'error': '', 'created_at': now()}
        if execution_options is not None:
            from .transports import job_options
            job['execution_options'] = job_options(execution_options)
        with self.connect(True) as db:
            self.require_snapshot(db, snapshot)
            db.execute('INSERT INTO questions VALUES(?,?,?)', (job['id'], canonical(job), job['created_at']))
        return job

    def save_question(self, job):
        with self.connect(True) as db:
            self.require_snapshot(db, job['snapshot'])
            if not db.execute('UPDATE questions SET payload=? WHERE id=?', (canonical(job), job['id'])).rowcount:
                raise Conflict('질문이 삭제되었습니다.')

    def end_unstored_question(self, key, error):
        """Moves a question stored as queued or running to error in one write transaction; a question stored as anything else is left as it is. Returns whether it moved."""
        with self.connect(True) as db:
            row = db.execute('SELECT payload FROM questions WHERE id=?', (key,)).fetchone()
            if not row:
                raise ValueError('질문을 찾을 수 없습니다.')
            job = json.loads(row[0])
            if job['status'] not in ('queued', 'running'):
                return False
            db.execute('UPDATE questions SET payload=? WHERE id=?', (canonical(job | {'status': 'error', 'error': error}), key))
            return True

    def question(self, key):
        with self.connect() as db:
            row = db.execute('SELECT payload FROM questions WHERE id=?', (key,)).fetchone()
            if not row:
                raise ValueError('질문을 찾을 수 없습니다.')
            return json.loads(row[0])

    def questions(self):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT payload FROM questions ORDER BY created_at DESC LIMIT 30')]

    def register(self, title, filename, raw, text, document_id=None, expected_version=None, expected_generation=None):
        with self.connect(True) as db:
            if document_id:
                row = db.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone()
                if not row:
                    raise ValueError('문서를 찾을 수 없습니다.')
                self.check_document_token(row, expected_version, expected_generation)
                self.refuse_while_run_needs_unchanged_document(db, document_id)
                version = row['next_version']
                db.execute('UPDATE documents SET current_version=?,next_version=?,generation=generation+1 WHERE id=?', (version, version + 1, document_id))
            else:
                document_id, version = ident(), 1
                db.execute('INSERT INTO documents(id,title,current_version,next_version,generation) VALUES(?,?,?,2,1)', (document_id, title, version))
            db.execute('INSERT INTO versions VALUES(?,?,?,?,?,?,?)',
                       (document_id, version, filename, text, raw, digest(raw), now()))
        return self.document(document_id)

    def documents(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT d.*,v.filename,v.sha256,v.created_at,(SELECT COUNT(*) FROM versions WHERE document_id=d.id) AS version_count FROM documents d JOIN versions v ON v.document_id=d.id AND v.version=d.current_version ORDER BY v.created_at DESC')]

    def document(self, document_id, version=None):
        with self.connect() as db:
            doc = db.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone()
            if not doc:
                raise ValueError('문서를 찾을 수 없습니다.')
            selected = version or doc['current_version']
            source = db.execute('SELECT document_id,version,filename,text,sha256,created_at FROM versions WHERE document_id=? AND version=?', (document_id, selected)).fetchone()
            if not source:
                raise ValueError('버전을 찾을 수 없습니다.')
            versions = [dict(r) for r in db.execute('SELECT version,filename,sha256,created_at FROM versions WHERE document_id=? ORDER BY version DESC', (document_id,))]
            items = [self.unpack(r) for r in db.execute('SELECT * FROM items WHERE document_id=? AND version=? ORDER BY kind,id', (document_id, selected))]
            runs = {r['id']: self.unpack_run(r) for r in db.execute('SELECT * FROM runs WHERE document_id=? AND version=?', (document_id, selected))}
            dictionary = next((i for i in items if i['kind'] == 'ontology'), None)
            extracted = set()
            for run in runs.values():
                if run['stage'] == 'extract' and run['status'] == 'success' and run['ontology']:
                    extracted.add(extraction_key(run['ontology']))
            for item in items:
                origin = runs.get(item['run_id'], {})
                pin = origin.get('ontology') if origin.get('stage') == 'extract' else None
                item['dictionary'] = {k: pin[k] for k in ('id', 'revision', 'sha256')} if pin else None
            can_extract = bool(dictionary and dictionary['status'] == 'approved' and extraction_key(dictionary | {'extraction_contract': EXTRACTION_CONTRACT_VERSION}) not in extracted)
            latest = max(runs.values(), key=lambda r: (r['created_at'], r['id']), default=None)
            latest_run = {k: latest[k] for k in ('id', 'stage', 'status', 'version')} if latest else None
            return dict(doc) | dict(source) | {'latest_run': latest_run, 'versions': versions, 'items': items, 'can_extract': can_extract, 'extraction_upgrade': bool(can_extract and dictionary is not None and any(k[:2] == (dictionary['id'], dictionary['revision']) for k in extracted))}

    @staticmethod
    def unpack(row):
        item = dict(row)
        item['payload'] = json.loads(item['payload'])
        return item

    def document_metadata(self, document_id, version):
        """Navigation for legacy snapshots without loading source text or live items."""
        with self.connect() as db:
            row = db.execute('SELECT d.id,d.title,v.version,v.filename,v.sha256,length(v.text) AS total_chars FROM documents d JOIN versions v ON d.id=v.document_id WHERE d.id=? AND v.version=?', (document_id, version)).fetchone()
            if not row:
                raise ValueError('문서를 찾을 수 없습니다.')
            return dict(row)

    def source_text(self, document_id, version):
        """Only the guarded question fallback calls this raw-text accessor."""
        with self.connect() as db:
            row = db.execute('SELECT text FROM versions WHERE document_id=? AND version=?', (document_id, version)).fetchone()
            if not row:
                raise ValueError('원문을 찾을 수 없습니다.')
            return row[0]

    def raw(self, document_id, version):
        with self.connect() as db:
            row = db.execute('SELECT raw,filename FROM versions WHERE document_id=? AND version=?', (document_id, version)).fetchone()
            if not row:
                raise ValueError('원문을 찾을 수 없습니다.')
            return bytes(row['raw']), row['filename']

    def settings(self):
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key='agents'").fetchone()
            return json.loads(row[0]) if row else {'selected': 'agy', 'models': {}}

    def save_settings(self, value):
        from .dictionary_context import validate_context_bytes
        value = dict(value)
        if 'dictionary_context_bytes' in value:
            validate_context_bytes(value['dictionary_context_bytes'])
        with self.connect(True) as db:
            if 'dictionary_context_bytes' not in value:
                previous = db.execute("SELECT value FROM settings WHERE key='agents'").fetchone()
                previous = json.loads(previous['value']) if previous else {}
                if 'dictionary_context_bytes' in previous:
                    value['dictionary_context_bytes'] = validate_context_bytes(previous['dictionary_context_bytes'])
            db.execute("INSERT OR REPLACE INTO settings VALUES('agents',?)", (canonical(value),))
        return value

    def usage_rates(self):
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key='usage_rates'").fetchone()
            return json.loads(row[0]) if row else {}

    def save_usage_rates(self, value):
        from .usage import validate_rates
        value = validate_rates(value)
        with self.connect(True) as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES('usage_rates',?)", (canonical(value),))
        return value

    def usage_owners(self, limit=1000):
        with self.connect() as db:
            runs = [dict(r) | {'kind': 'runs'} for r in db.execute('SELECT id,agent,model,stage,status,created_at FROM runs ORDER BY created_at DESC,id LIMIT ?', (limit + 1,))]
            questions = []
            for row in db.execute('SELECT payload FROM questions ORDER BY created_at DESC,id LIMIT ?', (limit + 1,)):
                job = json.loads(row[0])
                questions.append({k: job[k] for k in ('id', 'agent', 'model', 'status', 'created_at')} | {'kind': 'questions', 'stage': 'question'})
            total = db.execute('SELECT (SELECT count(*) FROM runs)+(SELECT count(*) FROM questions)').fetchone()[0]
        owners = sorted(runs + questions, key=lambda r: (r['created_at'], r['id']), reverse=True)
        return owners[:limit], total

    def create_run(self, document_id, agent, model, stage='extract', expected_version=None, expected_generation=None, *, execution_options=None, extraction_options=None, regenerate=None):
        if stage not in ('ontology', 'extract'):
            raise ValueError('지원하지 않는 추출 단계')
        if execution_options is not None:
            from .transports import job_options
            execution_options = job_options(execution_options)
        if extraction_options is not None:
            from .extraction_options import extraction_options as checked_extraction
            if execution_options is not None: raise ValueError('HTTP와 CLI 추출 옵션을 함께 사용할 수 없습니다.')
            extraction_options = checked_extraction(extraction_options, registered=False, agent=agent, stage=stage, model=model)
        if regenerate is not None:
            if (stage != 'ontology' or not isinstance(regenerate, dict) or set(regenerate) != {'id', 'revision'}
                    or not isinstance(regenerate['id'], str) or not regenerate['id'] or len(regenerate['id']) > 200
                    or type(regenerate['revision']) is not int or regenerate['revision'] < 1):
                raise ValueError('사전 재생성에는 id와 양의 정수 revision이 필요합니다.')
        run_id = ident()
        with self.connect(True) as db:
            doc = db.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone()
            if not doc:
                raise ValueError('문서를 찾을 수 없습니다.')
            self.check_document_token(doc, expected_version, expected_generation)
            dictionary = db.execute("SELECT * FROM items WHERE document_id=? AND version=? AND kind='ontology'", (document_id, doc['current_version'])).fetchone()
            pinned = None
            if stage == 'ontology':
                if regenerate is not None:
                    if (not dictionary or dictionary['id'] != regenerate['id']
                            or dictionary['revision'] != regenerate['revision'] or dictionary['status'] != 'rejected'):
                        raise Conflict('반려된 사전이 변경되었습니다. 새로고침 후 다시 생성하세요.')
                    pinned = {'id': dictionary['id'], 'revision': dictionary['revision'],
                              'sha256': digest(json.loads(dictionary['payload'])), 'run_id': dictionary['run_id'],
                              'generation': doc['generation']}
                elif dictionary:
                    raise Conflict('이미 사전 초안이 있습니다. 반려 후 다시 생성하거나 내용을 수정하세요.')
            else:
                if not dictionary or dictionary['status'] != 'approved':
                    raise Conflict('이 문서 버전의 개체·별칭·관계 사전을 먼저 승인하세요.')
                payload = json.loads(dictionary['payload'])
                pinned = {'id': dictionary['id'], 'revision': dictionary['revision'], 'payload': payload, 'sha256': digest(payload), 'extraction_contract': EXTRACTION_CONTRACT_VERSION}
                for prior in db.execute("SELECT ontology FROM runs WHERE document_id=? AND version=? AND stage='extract' AND status='success'", (document_id, doc['current_version'])):
                    old = json.loads(prior[0])
                    if extraction_key(old) == extraction_key(pinned):
                        raise Conflict('이 승인 사전 revision과 추출 방식으로 이미 추출했습니다. 사전 수정·재승인 후 다시 추출할 수 있으며, 모든 청크를 새로 호출하고 제외된 항목이 되살아난다는 보장은 없습니다.')
            if extraction_options is not None:
                from .extraction_options import bind_extraction_options
                source = db.execute('SELECT text FROM versions WHERE document_id=? AND version=?',
                                    (document_id, doc['current_version'])).fetchone()
                extraction_options = bind_extraction_options(extraction_options, source['text'], doc['generation'])
            try:
                db.execute('INSERT INTO runs(id,document_id,version,agent,model,status,detail,created_at,stage,ontology,execution_options,extraction_options) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                           (run_id, document_id, doc['current_version'], agent, model, 'queued', '', now(), stage, canonical(pinned) if pinned else None, canonical(execution_options) if execution_options is not None else None, canonical(extraction_options) if extraction_options is not None else None))
            except sqlite3.IntegrityError as exc:
                raise Conflict('이 문서는 이미 추출 중입니다.') from exc
        return self.run(run_id)

    @staticmethod
    def unpack_run(row):
        value = dict(row)
        value['ontology'] = json.loads(value['ontology']) if value['ontology'] else None
        value['execution_options'] = json.loads(value['execution_options']) if value.get('execution_options') else None
        if value.get('extraction_options') is not None:
            from .extraction_options import extraction_options
            value['extraction_options'] = extraction_options(json.loads(value['extraction_options']), agent=value['agent'], stage=value['stage'], model=value['model'])
        else:
            value['extraction_options'] = None
        return value

    def run(self, run_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if not row:
                raise ValueError('실행을 찾을 수 없습니다.')
            return self.unpack_run(row)

    def runs(self):
        with self.connect() as db:
            return [self.unpack_run(r) for r in db.execute('SELECT * FROM runs ORDER BY created_at DESC LIMIT 100')]

    def dictionary_retries(self, run):
        """A run cancelled while queued (its detail is QUEUED_CANCEL_DETAIL) never called a model or saved a checkpoint, so it is not one of the eight candidates; extraction_retries leaves it out the same way."""
        with self.connect() as db:
            return [self.unpack_run(r) for r in db.execute(
                "SELECT * FROM runs WHERE document_id=? AND version=? AND agent=? AND model=? AND stage='ontology' AND status IN ('error','interrupted','cancelled') AND NOT (status='cancelled' AND detail=?) AND id<>? AND ontology IS ? ORDER BY created_at DESC,id DESC LIMIT 8",
                (run['document_id'], run['version'], run['agent'], run['model'], QUEUED_CANCEL_DETAIL, run['id'], canonical(run['ontology']) if run['ontology'] else None))]

    def extraction_retries(self, run):
        with self.connect() as db:
            return [self.unpack_run(r) for r in db.execute(
                "SELECT * FROM runs WHERE document_id=? AND version=? AND agent=? AND model=? AND stage='extract' AND status IN ('error','interrupted','cancelled') AND NOT (status='cancelled' AND detail=?) AND id<>? ORDER BY created_at DESC,id DESC LIMIT 8",
                (run['document_id'], run['version'], run['agent'], run['model'], QUEUED_CANCEL_DETAIL, run['id']))]

    def run_status(self, run_id, status, detail=''):
        with self.connect(True) as db:
            if not db.execute("UPDATE runs SET status=?,detail=? WHERE id=? AND status NOT IN ('cancelling','cancelled')", (status, detail, run_id)).rowcount:
                if db.execute("SELECT 1 FROM runs WHERE id=? AND status IN ('cancelling','cancelled')", (run_id,)).fetchone():
                    raise RunCancelled('실행이 취소되었습니다.')

    def run_progress(self, run_id, detail):
        with self.connect(True) as db:
            db.execute("UPDATE runs SET detail=? WHERE id=? AND status='running'", (detail, run_id))

    def raise_if_cancelled(self, run_id):
        with self.connect() as db:
            if db.execute("SELECT 1 FROM runs WHERE id=? AND status IN ('cancelling','cancelled')", (run_id,)).fetchone():
                raise RunCancelled('실행이 취소되었습니다.')

    STATUS_LABELS = {'queued': '대기 중', 'running': '처리 중', 'success': '완료', 'error': '실패', 'interrupted': '중단', 'cancelling': '취소 중', 'cancelled': '취소됨'}

    def cancel_run(self, run_id, expected_status):
        """Cancel a run in one write transaction and return the status it had. A queued run becomes cancelled at once and keeps the detail it
        is given here for good: finish_cancel acts only on a cancelling run, so the detail of a run cancelled while queued, which says it never
        called a model, stays as it is (#857 relies on this marker). A running run becomes cancelling, and its worker moves it to cancelled when it stops. expected_status is what the
        caller saw; a run that has since changed is refused with a Conflict instead of being cancelled the other way."""
        if expected_status not in ('queued', 'running'):
            raise ValueError('expected_status는 queued 또는 running이어야 합니다.')
        with self.connect(True) as db:
            row = db.execute('SELECT status,stage FROM runs WHERE id=?', (run_id,)).fetchone()
            if not row: raise ValueError('실행을 찾을 수 없습니다.')
            status = row['status']
            if status == 'cancelling':
                raise Conflict('이미 취소를 요청한 실행입니다. 아직 멈추지 않았습니다.')
            if status not in ('queued', 'running'):
                raise Conflict(f"이미 끝난 실행이라 취소하지 않았습니다 (상태: {self.STATUS_LABELS.get(status, status)}).")
            if status != expected_status:
                raise Conflict('대기 중이던 실행이 처리 중으로 바뀌어 취소하지 않았습니다. 처리 중인 실행을 취소할지 다시 확인하세요.' if (expected_status, status) == ('queued', 'running')
                               else '실행 상태가 바뀌어 취소하지 않았습니다. 실행 기록을 새로고침하세요.')
            if status == 'queued':
                db.execute("UPDATE runs SET status='cancelled',detail=? WHERE id=?", (QUEUED_CANCEL_DETAIL, run_id))
            else:
                db.execute("UPDATE runs SET status='cancelling',detail=? WHERE id=?", ('취소 요청됨: 이미 허용한 호출은 중단하지 않고 끝날 때까지 기다린 뒤, 새 호출 없이 멈춥니다. 멈출 때까지 이 실행 뒤의 실행과 질문은 기다립니다.', run_id))
            return status

    def finish_cancel(self, run_id, detail):
        with self.connect(True) as db:
            db.execute("UPDATE runs SET status='cancelled',detail=? WHERE id=? AND status='cancelling'", (detail, run_id))

    def finish_run(self, run_id, page=None, facts=None, dictionary=None, detail=''):
        with self.connect(True) as db:
            run = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if not run or run['status'] != 'running':
                raise Conflict('실행 상태가 변경되었습니다.')
            self.refuse_publish_after_document_change(db, run)
            from .service import text, validate_fact, validate_page
            detail = text(detail, '실행 상세', 2000) if detail else ''
            from .ontology import dictionary_fact, validate_dictionary
            source = db.execute('SELECT text FROM versions WHERE document_id=? AND version=?', (run['document_id'], run['version'])).fetchone()[0]
            if run['stage'] == 'ontology':
                value = validate_dictionary(dictionary, source)
                target = json.loads(run['ontology']) if run['ontology'] else None
                if target:
                    current = db.execute("SELECT * FROM items WHERE id=? AND document_id=? AND version=? AND kind='ontology'",
                                         (target['id'], run['document_id'], run['version'])).fetchone()
                    if (not current or current['status'] != 'rejected' or current['revision'] != target['revision']
                            or digest(json.loads(current['payload'])) != target['sha256'] or current['run_id'] != target['run_id']):
                        raise Conflict('재생성 중 사전이 변경되었습니다. 새로고침 후 다시 생성하세요.')
                    revision = current['revision'] + 1
                    db.execute("UPDATE items SET payload=?,status='pending',revision=?,run_id=? WHERE id=?",
                               (canonical(value), revision, run_id, target['id']))
                    reason = f"사전 재생성 · 이전 실행 {target['run_id']} → 새 실행 {run_id}"
                    db.execute('INSERT INTO events(item_id,revision,action,actor,reason,payload,created_at) VALUES(?,?,?,?,?,?,?)',
                               (target['id'], revision, 'regenerated', 'agent:' + run['agent'], reason, canonical(value), now()))
                    db.execute("UPDATE runs SET status='success',detail=? WHERE id=?", (detail, run_id))
                    return
                if db.execute("SELECT 1 FROM items WHERE document_id=? AND version=? AND kind='ontology'", (run['document_id'], run['version'])).fetchone():
                    raise Conflict('이미 사전 초안이 있습니다.')
                candidates = [('ontology', value)]
            else:
                page = validate_page(page, source)
                if not isinstance(facts, list) or len(facts) > 1280:
                    raise ValueError('관계는 최대 1280개입니다.')
                if run['stage'] == 'extract':
                    pinned = json.loads(run['ontology'])
                    current = db.execute('SELECT * FROM items WHERE id=?', (pinned['id'],)).fetchone()
                    if not current or current['status'] != 'approved' or current['revision'] != pinned['revision'] or digest(json.loads(current['payload'])) != pinned['sha256']:
                        raise Conflict('추출 중 사전이 변경되거나 승인이 철회되었습니다. 다시 승인 후 추출하세요.')
                    facts = [dictionary_fact(f, source, pinned['payload']) for f in facts]
                else:
                    facts = [validate_fact(f, source) for f in facts]
                candidates = [('page', page), *[('fact', f) for f in facts]]
            for kind, payload in candidates:
                item_id = ident()
                db.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?)', (item_id, run['document_id'], run['version'], kind, canonical(payload), 'pending', 1, run_id))
                db.execute('INSERT INTO events(item_id,revision,action,actor,reason,payload,created_at) VALUES(?,?,?,?,?,?,?)', (item_id, 1, 'created', 'agent:' + run['agent'], '추출 후보', canonical(payload), now()))
            db.execute("UPDATE runs SET status='success',detail=? WHERE id=?", (detail, run_id))

    @staticmethod
    def validate_reviewer(actor, reason):
        if not isinstance(actor, str) or not actor.strip() or len(actor) > 100 or not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise ValueError('검토자(100자 이하)와 이유(1000자 이하)가 필요합니다.')

    def review(self, item_id, revision, action, actor, reason, payload=None):
        if action not in {'approved', 'rejected', 'held', 'pending', 'revise'}:
            raise ValueError('지원하지 않는 검토 상태')
        self.validate_reviewer(actor, reason)
        with self.connect(True) as db:
            row = db.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone()
            if not row:
                raise ValueError('후보를 찾을 수 없습니다.')
            return self._review_item(db, row, revision, action, actor, reason, payload)

    @staticmethod
    def run_wait_advice(status, retry, stage):
        """The sentences shared by the refusals that tell a user to wait for a queued or running run or to cancel it: the queue note (queued
        only), where to see it end, the cancel alternative, and, for a running run, that its call in flight continues without saving a result.
        retry is the user's next step as a stem, such as '다시 시도': the sentences end it with '하세요.' and '할 수 있고'."""
        waits = '대기 중인 실행은 앞선 실행이나 질문이 끝나야 시작합니다. ' if status == 'queued' else ''
        keeps = f' 처리 중이던 호출은 끝날 때까지 계속되지만 그 결과로 {cancel_result(stage, False)} 저장하지는 않습니다.' if status == 'running' else ''
        return (f'{waits}기다리려면 실행 기록에서 끝난 것을 확인한 뒤 {retry}하세요. 기다리지 않으려면 실행 기록에서 이 실행을 취소하세요. '
                f'취소하면 바로 {retry}할 수 있고, 다른 실행과 질문은 계속됩니다.{keeps}')

    @staticmethod
    def refuse_while_run_holds_dictionary(db, document_id, version):
        """Inside the caller's write transaction: a queued or running run on this document version pins its dictionary, so a change is refused until the run ends or is cancelled."""
        run = db.execute("SELECT id,stage,agent,model,status FROM runs WHERE document_id=? AND version=? AND status IN ('queued','running')",
                         (document_id, version)).fetchone()
        if run:
            job = '관계 추출' if run['stage'] == 'extract' else '사전 재생성'
            state = '대기 중' if run['status'] == 'queued' else '처리 중'
            raise Conflict(f"이 문서 버전의 {job} 실행 {run['id']}({run['agent']} / {run['model'] or '기본 모델'} · {state})이 끝나지 않아 사전을 바꿀 수 없습니다. "
                           f"지금 바꾸면 호출 비용을 쓴 결과를 저장하지 못합니다. {Store.run_wait_advice(run['status'], '다시 시도', run['stage'])}")

    @staticmethod
    def needs_unchanged_document(run):
        """True for a run that saves its result only if no source version of its document is registered or deleted between its request and its end."""
        return run['extraction_options'] is not None or (run['stage'] == 'ontology' and run['ontology'] is not None)

    @staticmethod
    def refuse_while_run_needs_unchanged_document(db, document_id):
        """Inside the caller's write transaction: a queued or running run of this document that needs it unchanged makes a new source version refused until the run ends or is cancelled."""
        run = db.execute("SELECT * FROM runs WHERE document_id=? AND status IN ('queued','running')", (document_id,)).fetchone()
        if run and Store.needs_unchanged_document(run):
            job = '병렬 수나 청크 임계 정책을 등록한 관계 추출' if run['stage'] == 'extract' else '사전 재생성'
            state = '대기 중' if run['status'] == 'queued' else '처리 중'
            raise Conflict(f"이 문서의 {job} 실행 {run['id']}({run['agent']} / {run['model'] or '기본 모델'} · {state})이 끝나지 않아 새 원문 버전을 등록할 수 없습니다. "
                           '이 실행은 요청된 때부터 끝날 때까지 이 문서에 원문 버전 등록이나 삭제가 없어야 결과를 저장하므로, 지금 등록하면 호출 비용을 쓴 결과를 저장하지 못합니다. '
                           + Store.run_wait_advice(run['status'], '다시 등록', run['stage']))

    @staticmethod
    def refuse_publish_after_document_change(db, run):
        """Inside finish_run's write transaction: a run that needs its document unchanged saves nothing once the current version or the generation differs from the one pinned at its request."""
        if not Store.needs_unchanged_document(run):
            return
        if run['extraction_options'] is not None:
            from .extraction_options import extraction_options as checked_extraction
            pinned = checked_extraction(json.loads(run['extraction_options']), agent=run['agent'], stage=run['stage'], model=run['model'])['document_generation']
        else:
            pinned = json.loads(run['ontology'])['generation']
        doc = db.execute('SELECT current_version,generation FROM documents WHERE id=?', (run['document_id'],)).fetchone()
        if doc is None:
            raise Conflict('실행이 요청된 뒤 문서가 삭제되어 결과를 저장하지 않았습니다.')
        if doc['current_version'] != run['version']:
            raise Conflict(f"실행이 요청된 뒤 이 문서의 현재 원문 버전이 바뀌어(실행 버전 {run['version']}, 현재 버전 {doc['current_version']}) 결과를 저장하지 않았습니다. "
                           f"새 실행은 현재 버전에서만 시작하므로 버전 {run['version']}에서는 다시 실행할 수 없습니다. 현재 버전에서 사전부터 다시 진행하세요.")
        if doc['generation'] != pinned:
            raise Conflict(f"실행이 요청된 뒤 이 문서의 원문 버전 기록이 바뀌어(문서 세대 {pinned} → {doc['generation']}) 결과를 저장하지 않았습니다. "
                           '현재 버전이 실행 버전과 같으므로 같은 버전에서 다시 실행할 수 있습니다.')

    def _review_item(self, db, row, revision, action, actor, reason, payload=None):
        """Shared validation and audit mutation inside the caller's transaction."""
        item_id = row['id']
        if row['kind'] == 'ontology':
            self.refuse_while_run_holds_dictionary(db, row['document_id'], row['version'])
        if row['revision'] != revision:
            raise Conflict('후보가 변경되었습니다. 새로고침 후 검토하세요.')
        value = json.loads(row['payload']) if payload is None else payload
        if payload is not None and (action != 'revise' or row['kind'] not in ('fact', 'ontology')):
            raise ValueError('사실·사전 수정 작업만 payload를 받습니다.')
        if action == 'revise' and payload is None:
            raise ValueError('수정할 사실이 필요합니다.')
        source = db.execute('SELECT text FROM versions WHERE document_id=? AND version=?', (row['document_id'], row['version'])).fetchone()[0]
        from .ontology import (
            dictionary_fact,
            refuse_new_lookup_spelling_problems,
            validate_dictionary,
        )
        from .service import validate_fact, validate_page
        if row['kind'] == 'ontology':
            value = validate_dictionary(value, source)
            if action == 'revise':
                refuse_new_lookup_spelling_problems(json.loads(row['payload']), payload)
        elif row['kind'] == 'fact':
            run = db.execute('SELECT stage,ontology FROM runs WHERE id=?', (row['run_id'],)).fetchone()
            value = dictionary_fact(value, source, json.loads(run['ontology'])['payload']) if run and run['stage'] == 'extract' else validate_fact(value, source)
        else:
            value = validate_page(value, source)
        status = 'pending' if action == 'revise' else action
        if status == row['status'] and action != 'revise':
            raise Conflict('이미 같은 상태입니다.')
        db.execute('UPDATE items SET payload=?,status=?,revision=revision+1 WHERE id=?', (canonical(value), status, item_id))
        db.execute('INSERT INTO events(item_id,revision,action,actor,reason,payload,created_at) VALUES(?,?,?,?,?,?,?)', (item_id, revision + 1, action, actor.strip(), reason.strip(), canonical(value), now()))
        return {'id': item_id, 'revision': revision + 1, 'status': status}

    def add_dictionary_term(self, item_id, revision, expected_version, expected_generation, kind, spelling, quote, actor, reason, *, entity=None, name=None, entity_type=None):
        from .ontology import lookup_spelling_problem
        from .service import evidence, text
        self.validate_reviewer(actor, reason)
        if kind not in ('alias', 'entity'):
            raise ValueError('별칭 또는 새 개체만 추가할 수 있습니다.')
        spelling = text(spelling, '표기')
        with self.connect(True) as db:
            row = db.execute("SELECT * FROM items WHERE id=? AND kind='ontology'", (item_id,)).fetchone()
            if not row:
                raise ValueError('사전 후보를 찾을 수 없습니다.')
            doc = db.execute('SELECT * FROM documents WHERE id=?', (row['document_id'],)).fetchone()
            self.check_document_token(doc, expected_version, expected_generation)
            if row['version'] != doc['current_version'] or row['status'] == 'rejected':
                raise Conflict('현재 문서 버전의 반려되지 않은 사전에만 추가할 수 있습니다.')
            source = db.execute('SELECT text FROM versions WHERE document_id=? AND version=?', (row['document_id'], row['version'])).fetchone()[0]
            quote = evidence(quote, source)
            payload = json.loads(row['payload'])
            entities = {e['id']: e for e in payload['entities']}
            if spelling in entities or any(a['alias'] == spelling for a in payload['aliases']):
                raise Conflict('이미 사전에 있는 표기입니다.')
            problem = lookup_spelling_problem('aliases' if kind == 'alias' else 'entities', spelling)
            if problem == 'prefix':
                raise ValueError('이 표기는 조회 참조 형식이라 사전에 추가할 수 없습니다.')
            if kind == 'alias':
                target = entities.get(entity) if isinstance(entity, str) else None
                if target is None:
                    raise ValueError('별칭이 가리킬 기존 개체를 고르세요.')
                added = {'alias': spelling, 'entity': entity, 'quote': quote}
                if target['name'] not in quote:
                    added |= {'judged_alias': spelling, 'judged_entity': entity, 'judged_name': target['name']}
                payload['aliases'].append(added)
                note = f'사전 추가(별칭): {spelling} → {entity}'
            else:
                if problem == 'control':
                    raise ValueError('줄바꿈이나 탭이 있는 표기는 개체 ID로 쓸 수 없습니다. 별칭으로 추가하세요.')
                if not isinstance(entity_type, str) or entity_type not in {e['type'] for e in payload['entities']}:
                    raise ValueError('사전에 있는 개체 유형을 고르세요.')
                payload['entities'].append({'id': spelling, 'name': text(name if name is not None else spelling, 'name'), 'type': entity_type, 'quote': quote})
                note = f'사전 추가(새 개체): {spelling} · {entity_type}'
            return self._review_item(db, row, revision, 'revise', actor, f'{note} · {reason.strip()}', payload)

    def spelling_passages(self, document_id, version, spelling, *, radius=80, limit=20):
        """Exact occurrences of `spelling` in one stored version, overlapping ones included: (passages, total). Never whitespace-tolerant."""
        from .service import text
        spelling = text(spelling, '표기')
        with self.connect() as db:
            row = db.execute('SELECT text FROM versions WHERE document_id=? AND version=?', (document_id, version)).fetchone()
        if not row:
            raise ValueError('버전을 찾을 수 없습니다.')
        source, found, at = row[0], [], row[0].find(spelling)
        while at >= 0:
            found.append(at)
            at = source.find(spelling, at + 1)
        return [{'offset': at, 'quote': source[max(0, at - radius):at + len(spelling) + radius]} for at in found[:limit]], len(found)

    def alias_judgements(self, item_id, payload):
        """Who last judged each judged alias row, from the item's events. Empty and unread when the payload has none."""
        judged = {row['alias']: row for row in payload['aliases'] if 'judged_name' in row}
        fields = ('judged_alias', 'judged_entity', 'judged_name')
        if not judged:
            return {}
        result: dict[str, dict] = {}
        previous: dict[str, dict] = {}
        with self.connect() as db:
            for event in db.execute('SELECT revision,actor,reason,payload,created_at FROM events WHERE item_id=? ORDER BY seq', (item_id,)):
                rows = {row['alias']: row for row in json.loads(event['payload'])['aliases']}
                for alias, row in rows.items():
                    if 'judged_name' in row and any(previous.get(alias, {}).get(field) != row[field] for field in fields):
                        result[alias] = {'actor': event['actor'], 'reason': event['reason'], 'revision': event['revision'], 'created_at': event['created_at']}
                previous = rows
        return {alias: result[alias] for alias in judged if alias in result}

    def bulk_approve(self, document_id, version, expected_version, expected_generation, items, actor, reason):
        self.validate_reviewer(actor, reason)
        if type(version) is not int or version < 1:
            raise ValueError('승인할 문서 version은 양의 정수여야 합니다.')
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_BULK_APPROVAL:
            raise ValueError(f'일괄 승인은 1~{MAX_BULK_APPROVAL}개 항목을 선택해야 합니다.')
        seen = set()
        for item in items:
            if not isinstance(item, dict) or set(item) != {'id', 'revision'}:
                raise ValueError('각 승인 대상에는 id와 revision만 필요합니다.')
            if not isinstance(item['id'], str) or not item['id'] or len(item['id']) > 200 or type(item['revision']) is not int or item['revision'] < 1:
                raise ValueError('유효한 후보 ID와 양의 정수 revision이 필요합니다.')
            if item['id'] in seen:
                raise ValueError('일괄 승인 대상 ID가 중복되었습니다.')
            seen.add(item['id'])
        with self.connect(True) as db:
            doc = db.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone()
            if not doc:
                raise Conflict('문서가 삭제되었습니다. 목록을 새로고침하세요.')
            self.check_document_token(doc, expected_version, expected_generation)
            if not db.execute('SELECT 1 FROM versions WHERE document_id=? AND version=?', (document_id, version)).fetchone():
                raise Conflict('문서 버전이 삭제되었습니다. 목록을 새로고침하세요.')
            rows = []
            for item in items:
                row = db.execute('SELECT * FROM items WHERE id=?', (item['id'],)).fetchone()
                if not row or row['document_id'] != document_id or row['version'] != version:
                    raise Conflict('선택한 문서 버전에 속하지 않거나 삭제된 후보가 있습니다. 다시 선택하세요.')
                if row['kind'] not in ('page', 'fact'):
                    raise ValueError('일괄 승인은 위키·관계만 가능합니다. 사전은 별도로 승인하세요.')
                if row['revision'] != item['revision'] or row['status'] != 'pending':
                    raise Conflict('선택한 후보가 변경되었거나 검토 대기가 아닙니다. 새로고침 후 다시 선택하세요.')
                rows.append(row)
            approved = [self._review_item(db, row, row['revision'], 'approved', actor, reason) for row in rows]
        return {'document_id': document_id, 'version': version, 'approved_count': len(approved), 'items': approved}

    def history(self, item_id):
        with self.connect() as db:
            return [self.unpack(r) for r in db.execute('SELECT * FROM events WHERE item_id=? ORDER BY seq', (item_id,))]

    def snapshot(self, snapshot_id=None):
        with self.connect(True) as db:
            if snapshot_id:
                row = db.execute('SELECT payload FROM snapshots WHERE id=?', (snapshot_id,)).fetchone()
                if not row:
                    raise ValueError('스냅샷을 찾을 수 없습니다.')
                return json.loads(row[0])
            approved: dict[str, list[dict]] = {'fact': [], 'page': [], 'ontology': []}
            for row in db.execute("SELECT i.*,v.sha256 FROM items i JOIN documents d ON i.document_id=d.id AND i.version=d.current_version JOIN versions v ON v.document_id=i.document_id AND v.version=i.version WHERE i.kind IN ('fact','page','ontology') AND i.status='approved' ORDER BY i.id"):
                approved[row['kind']].append(self.unpack(row))
            documents = [dict(r) for r in db.execute('SELECT d.id,d.current_version,d.title,v.filename,v.sha256,length(v.text) AS total_chars FROM documents d JOIN versions v ON v.document_id=d.id AND v.version=d.current_version ORDER BY d.id')]
            # A reviewed fact keeps the extraction-time meaning even after its
            # dictionary is revised or revoked. Never substitute today's pin.
            fact_ontologies = {}; ontology_pins = {}; run_pins = {}
            for fact in approved['fact']:
                if fact['run_id'] not in run_pins:
                    origin = db.execute('SELECT document_id,version,stage,ontology FROM runs WHERE id=?', (fact['run_id'],)).fetchone()
                    pin = None; pin_ref = None
                    if origin and origin['stage'] == 'extract' and origin['ontology']:
                        try: pin = json.loads(origin['ontology'])
                        except (ValueError, TypeError): pass
                    if pin is not None:
                        pin_ref = digest({'document_id': origin['document_id'], 'version': origin['version'], 'pin': pin})
                        ontology_pins[pin_ref] = pin
                    run_pins[fact['run_id']] = (origin, pin_ref)
                origin, pin_ref = run_pins[fact['run_id']]
                fact_ontologies[fact['id']] = (pin_ref if origin and origin['document_id'] == fact['document_id']
                                               and origin['version'] == fact['version'] else None)
            body = {'facts': approved['fact'], 'pages': approved['page'], 'ontologies': approved['ontology'], 'knowledge_version': 1,
                    'documents': documents, 'graph_identity_version': 1, 'fact_ontologies': fact_ontologies, 'ontology_pins': ontology_pins}
            key = digest(body)
            value = {'id': key, **body}
            db.execute('INSERT OR IGNORE INTO snapshots VALUES(?,?,?)', (key, canonical(value), now()))
            return value

    def save_query(self, result):
        key = ident()
        with self.connect(True) as db:
            self.require_snapshot(db, result['snapshot'])
            db.execute('INSERT INTO queries VALUES(?,?,?)', (key, canonical(result), now()))
        return key

    def queries(self):
        with self.connect() as db:
            return [self.unpack(r) for r in db.execute('SELECT * FROM queries ORDER BY created_at DESC LIMIT 30')]

    @staticmethod
    def require_snapshot(db, snapshot_id):
        if not db.execute('SELECT 1 FROM snapshots WHERE id=?', (snapshot_id,)).fetchone():
            raise Conflict('근거 스냅샷이 삭제되었습니다. 새로 조회하세요.')

    @staticmethod
    def check_document_token(row, version, generation):
        if type(version) is not int or version < 1 or type(generation) is not int or generation < 1:
            raise ValueError('expected_version과 expected_generation 양의 정수가 필요합니다. 문서를 새로고침하세요.')
        if row['current_version'] != version or row['generation'] != generation:
            raise Conflict('문서가 변경되었습니다. 새로고침 후 다시 선택하세요.')

    def delete_document(self, document_id, latest_only, expected_version, expected_generation):
        with self.connect(True) as db:
            doc = db.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone()
            if not doc:
                raise ValueError('문서를 찾을 수 없습니다.')
            self.check_document_token(doc, expected_version, expected_generation)
            cancelling = db.execute("SELECT 1 FROM runs WHERE status='cancelling' LIMIT 1").fetchone()
            waiting = (db.execute("SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1").fetchone()
                       or any(json.loads(r[0])['status'] in ('queued', 'running') for r in db.execute('SELECT payload FROM questions')))
            if cancelling and not waiting:
                raise Conflict('취소한 실행이 아직 멈추지 않았습니다. 실행 기록에서 취소됨으로 바뀐 뒤 삭제하세요. '
                               '제한 시간(추출 청크 360초, 기본 사전 생성 호출 600초)이 지나도 바뀌지 않으면 서버를 재시작하세요. 재시작하면 대기·처리 중인 모든 실행과 질문이 중단으로 표시됩니다.')
            if waiting:
                raise Conflict('추출 또는 질문 작업이 진행 중입니다. 모든 작업 완료 후 삭제하세요.')
            versions = [r[0] for r in db.execute('SELECT version FROM versions WHERE document_id=? ORDER BY version', (document_id,))]
            if latest_only and len(versions) == 1:
                raise Conflict('리비전이 하나뿐입니다. 문서 전체 삭제를 사용하세요.')
            removed = [doc['current_version']] if latest_only else versions
            snapshots = {r['id'] for r in db.execute('SELECT id,payload FROM snapshots') if any(d['id'] == document_id and d['current_version'] in removed for d in json.loads(r['payload'])['documents'])}
            question_ids = [r['id'] for r in db.execute('SELECT id,payload FROM questions') if json.loads(r['payload'])['snapshot'] in snapshots]
            query_ids = [r['id'] for r in db.execute('SELECT id,payload FROM queries') if json.loads(r['payload'])['snapshot'] in snapshots]
            run_ids = [r['id'] for r in db.execute('SELECT id,version FROM runs WHERE document_id=?', (document_id,)) if r['version'] in removed]
            db.executemany('INSERT OR IGNORE INTO log_cleanup VALUES(?,?)', [('runs', key) for key in run_ids] + [('questions', key) for key in question_ids])
            for table, keys in [('questions', question_ids), ('queries', query_ids), ('snapshots', snapshots), ('runs', run_ids)]:
                db.executemany(f'DELETE FROM {table} WHERE id=?', [(key,) for key in keys])
            for version in removed:
                db.execute('DELETE FROM events WHERE item_id IN (SELECT id FROM items WHERE document_id=? AND version=?)', (document_id, version))
                db.execute('DELETE FROM items WHERE document_id=? AND version=?', (document_id, version))
                db.execute('DELETE FROM versions WHERE document_id=? AND version=?', (document_id, version))
            current = max(v for v in versions if v not in removed) if latest_only else None
            if latest_only:
                db.execute('UPDATE documents SET current_version=?,generation=generation+1 WHERE id=?', (current, document_id))
            else:
                db.execute('DELETE FROM documents WHERE id=?', (document_id,))
        return {'document_id': document_id, 'removed_versions': removed, 'current_version': current,
                'removed_snapshots': len(snapshots), 'removed_questions': len(question_ids), 'removed_queries': len(query_ids)}

    def pending_cleanup(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM log_cleanup ORDER BY kind,id')]

    def finish_cleanup(self, kind, key):
        with self.connect(True) as db:
            db.execute('DELETE FROM log_cleanup WHERE kind=? AND id=?', (kind, key))
