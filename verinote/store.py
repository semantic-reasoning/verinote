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
                pin = runs.get(item['run_id'], {}).get('ontology')
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
        with self.connect(True) as db:
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

    def create_run(self, document_id, agent, model, stage='extract', expected_version=None, expected_generation=None, *, execution_options=None):
        if stage not in ('ontology', 'extract'):
            raise ValueError('지원하지 않는 추출 단계')
        if execution_options is not None:
            from .transports import job_options
            execution_options = job_options(execution_options)
        run_id = ident()
        with self.connect(True) as db:
            doc = db.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone()
            if not doc:
                raise ValueError('문서를 찾을 수 없습니다.')
            self.check_document_token(doc, expected_version, expected_generation)
            dictionary = db.execute("SELECT * FROM items WHERE document_id=? AND version=? AND kind='ontology'", (document_id, doc['current_version'])).fetchone()
            pinned = None
            if stage == 'ontology':
                if dictionary:
                    raise Conflict('이미 사전 초안이 있습니다. 사전을 수정하고 다시 승인하세요.')
            else:
                if not dictionary or dictionary['status'] != 'approved':
                    raise Conflict('이 문서 버전의 개체·별칭·관계 사전을 먼저 승인하세요.')
                payload = json.loads(dictionary['payload'])
                pinned = {'id': dictionary['id'], 'revision': dictionary['revision'], 'payload': payload, 'sha256': digest(payload), 'extraction_contract': EXTRACTION_CONTRACT_VERSION}
                for prior in db.execute("SELECT ontology FROM runs WHERE document_id=? AND version=? AND stage='extract' AND status='success'", (document_id, doc['current_version'])):
                    old = json.loads(prior[0])
                    if extraction_key(old) == extraction_key(pinned):
                        raise Conflict('이 승인 사전 revision과 추출 방식으로 이미 추출했습니다. 사전 수정·재승인 후 다시 추출하세요.')
            try:
                db.execute('INSERT INTO runs(id,document_id,version,agent,model,status,detail,created_at,stage,ontology,execution_options) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                           (run_id, document_id, doc['current_version'], agent, model, 'queued', '', now(), stage, canonical(pinned) if pinned else None, canonical(execution_options) if execution_options is not None else None))
            except sqlite3.IntegrityError as exc:
                raise Conflict('이 문서는 이미 추출 중입니다.') from exc
        return self.run(run_id)

    @staticmethod
    def unpack_run(row):
        value = dict(row)
        value['ontology'] = json.loads(value['ontology']) if value['ontology'] else None
        value['execution_options'] = json.loads(value['execution_options']) if value.get('execution_options') else None
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
        with self.connect() as db:
            return [self.unpack_run(r) for r in db.execute(
                "SELECT * FROM runs WHERE document_id=? AND version=? AND agent=? AND model=? AND stage='ontology' AND status IN ('error','interrupted') AND id<>? ORDER BY created_at DESC,id DESC LIMIT 8",
                (run['document_id'], run['version'], run['agent'], run['model'], run['id']))]

    def extraction_retries(self, run):
        with self.connect() as db:
            return [self.unpack_run(r) for r in db.execute(
                "SELECT * FROM runs WHERE document_id=? AND version=? AND agent=? AND model=? AND stage='extract' AND status IN ('error','interrupted') AND id<>? ORDER BY created_at DESC,id DESC LIMIT 8",
                (run['document_id'], run['version'], run['agent'], run['model'], run['id']))]

    def run_status(self, run_id, status, detail=''):
        with self.connect(True) as db:
            db.execute('UPDATE runs SET status=?,detail=? WHERE id=?', (status, detail, run_id))

    def finish_run(self, run_id, page=None, facts=None, dictionary=None, detail=''):
        with self.connect(True) as db:
            run = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if not run or run['status'] != 'running':
                raise Conflict('실행 상태가 변경되었습니다.')
            from .service import text, validate_fact, validate_page
            detail = text(detail, '실행 상세', 2000) if detail else ''
            from .ontology import dictionary_fact, validate_dictionary
            source = db.execute('SELECT text FROM versions WHERE document_id=? AND version=?', (run['document_id'], run['version'])).fetchone()[0]
            if run['stage'] == 'ontology':
                if db.execute("SELECT 1 FROM items WHERE document_id=? AND version=? AND kind='ontology'", (run['document_id'], run['version'])).fetchone():
                    raise Conflict('이미 사전 초안이 있습니다.')
                candidates = [('ontology', validate_dictionary(dictionary, source))]
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

    def _review_item(self, db, row, revision, action, actor, reason, payload=None):
        """Shared validation and audit mutation inside the caller's transaction."""
        item_id = row['id']
        if row['revision'] != revision:
            raise Conflict('후보가 변경되었습니다. 새로고침 후 검토하세요.')
        value = json.loads(row['payload']) if payload is None else payload
        if payload is not None and (action != 'revise' or row['kind'] not in ('fact', 'ontology')):
            raise ValueError('사실·사전 수정 작업만 payload를 받습니다.')
        if action == 'revise' and payload is None:
            raise ValueError('수정할 사실이 필요합니다.')
        source = db.execute('SELECT text FROM versions WHERE document_id=? AND version=?', (row['document_id'], row['version'])).fetchone()[0]
        from .ontology import dictionary_fact, validate_dictionary
        from .service import validate_fact, validate_page
        if row['kind'] == 'ontology':
            value = validate_dictionary(value, source)
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
            approved: dict[str, list[object]] = {'fact': [], 'page': [], 'ontology': []}
            for row in db.execute("SELECT i.*,v.sha256 FROM items i JOIN documents d ON i.document_id=d.id AND i.version=d.current_version JOIN versions v ON v.document_id=i.document_id AND v.version=i.version WHERE i.kind IN ('fact','page','ontology') AND i.status='approved' ORDER BY i.id"):
                approved[row['kind']].append(self.unpack(row))
            documents = [dict(r) for r in db.execute('SELECT d.id,d.current_version,d.title,v.filename,v.sha256,length(v.text) AS total_chars FROM documents d JOIN versions v ON v.document_id=d.id AND v.version=d.current_version ORDER BY d.id')]
            body = {'facts': approved['fact'], 'pages': approved['page'], 'ontologies': approved['ontology'], 'knowledge_version': 1, 'documents': documents}
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
            if db.execute("SELECT 1 FROM runs WHERE status IN ('queued','running') LIMIT 1").fetchone() or any(json.loads(r[0])['status'] in ('queued', 'running') for r in db.execute('SELECT payload FROM questions')):
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
