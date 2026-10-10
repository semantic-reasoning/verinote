"""Transactional immutable document versions and optimistic human review."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid


def now():
    return datetime.now(timezone.utc).isoformat()


def ident():
    return uuid.uuid4().hex


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value).encode()).hexdigest()


class Conflict(ValueError):
    pass


SCHEMA = '''
CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY,title TEXT NOT NULL,current_version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS versions(document_id TEXT,version INTEGER,filename TEXT,text TEXT,raw BLOB,sha256 TEXT,created_at TEXT,PRIMARY KEY(document_id,version));
CREATE TABLE IF NOT EXISTS items(id TEXT PRIMARY KEY,document_id TEXT,version INTEGER,kind TEXT,payload TEXT,status TEXT,revision INTEGER,run_id TEXT);
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,item_id TEXT,revision INTEGER,action TEXT,actor TEXT,reason TEXT,payload TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,document_id TEXT,version INTEGER,agent TEXT,model TEXT,status TEXT,detail TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE IF NOT EXISTS snapshots(id TEXT PRIMARY KEY,payload TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS queries(id TEXT PRIMARY KEY,payload TEXT,created_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS active_extraction ON runs(document_id) WHERE status IN ('queued','running');
'''


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)

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

    def register(self, title, filename, raw, text, document_id=None, expected_version=None):
        with self.connect(True) as db:
            if document_id:
                row = db.execute('SELECT * FROM documents WHERE id=?', (document_id,)).fetchone()
                if not row:
                    raise ValueError('문서를 찾을 수 없습니다.')
                if row['current_version'] != expected_version:
                    raise Conflict('문서 버전이 변경되었습니다. 새로고침하세요.')
                version = row['current_version'] + 1
                db.execute('UPDATE documents SET current_version=? WHERE id=?', (version, document_id))
            else:
                document_id, version = ident(), 1
                db.execute('INSERT INTO documents VALUES(?,?,?)', (document_id, title, version))
            db.execute('INSERT INTO versions VALUES(?,?,?,?,?,?,?)',
                       (document_id, version, filename, text, raw, digest(raw), now()))
        return self.document(document_id)

    def documents(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT d.*,v.filename,v.sha256,v.created_at FROM documents d JOIN versions v ON v.document_id=d.id AND v.version=d.current_version ORDER BY v.created_at DESC')]

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
            return dict(doc) | dict(source) | {'versions': versions, 'items': items}

    @staticmethod
    def unpack(row):
        item = dict(row)
        item['payload'] = json.loads(item['payload'])
        return item

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

    def create_run(self, document_id, agent, model):
        run_id = ident()
        with self.connect(True) as db:
            doc = db.execute('SELECT current_version FROM documents WHERE id=?', (document_id,)).fetchone()
            if not doc:
                raise ValueError('문서를 찾을 수 없습니다.')
            # Retrying is permitted after failure; successful extraction is never silently replaced.
            if db.execute('SELECT 1 FROM items WHERE document_id=? AND version=? LIMIT 1', (document_id, doc[0])).fetchone():
                raise Conflict('이 버전은 이미 추출되었습니다. 후보를 수정하거나 새 문서 버전을 등록하세요.')
            try:
                db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)', (run_id, document_id, doc[0], agent, model, 'queued', '', now()))
            except sqlite3.IntegrityError as exc:
                raise Conflict('이 문서는 이미 추출 중입니다.') from exc
        return self.run(run_id)

    def run(self, run_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if not row:
                raise ValueError('실행을 찾을 수 없습니다.')
            return dict(row)

    def runs(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM runs ORDER BY created_at DESC LIMIT 100')]

    def run_status(self, run_id, status, detail=''):
        with self.connect(True) as db:
            db.execute('UPDATE runs SET status=?,detail=? WHERE id=?', (status, detail, run_id))

    def finish_run(self, run_id, page, facts):
        with self.connect(True) as db:
            run = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if run['status'] != 'running':
                raise Conflict('실행 상태가 변경되었습니다.')
            for kind, payload in [('page', page), *[('fact', f) for f in facts]]:
                item_id = ident()
                db.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?)', (item_id, run['document_id'], run['version'], kind, canonical(payload), 'pending', 1, run_id))
                db.execute('INSERT INTO events(item_id,revision,action,actor,reason,payload,created_at) VALUES(?,?,?,?,?,?,?)', (item_id, 1, 'created', 'agent:' + run['agent'], '추출 후보', canonical(payload), now()))
            db.execute("UPDATE runs SET status='success',detail='' WHERE id=?", (run_id,))

    def review(self, item_id, revision, action, actor, reason, payload=None):
        if action not in {'approved', 'rejected', 'held', 'pending', 'revise'}:
            raise ValueError('지원하지 않는 검토 상태')
        if not isinstance(actor, str) or not actor.strip() or len(actor) > 100 or not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise ValueError('검토자(100자 이하)와 이유(1000자 이하)가 필요합니다.')
        with self.connect(True) as db:
            row = db.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone()
            if not row:
                raise ValueError('후보를 찾을 수 없습니다.')
            if row['revision'] != revision:
                raise Conflict('후보가 변경되었습니다. 새로고침 후 검토하세요.')
            value = json.loads(row['payload']) if payload is None else payload
            if payload is not None and (action != 'revise' or row['kind'] != 'fact'):
                raise ValueError('사실 수정 작업만 payload를 받습니다.')
            if action == 'revise' and payload is None:
                raise ValueError('수정할 사실이 필요합니다.')
            source = db.execute('SELECT text FROM versions WHERE document_id=? AND version=?', (row['document_id'], row['version'])).fetchone()[0]
            from .service import validate_fact, validate_page
            value = validate_fact(value, source) if row['kind'] == 'fact' else validate_page(value, source)
            status = 'pending' if action == 'revise' else action
            if status == row['status'] and action != 'revise':
                raise Conflict('이미 같은 상태입니다.')
            db.execute('UPDATE items SET payload=?,status=?,revision=revision+1 WHERE id=?', (canonical(value), status, item_id))
            db.execute('INSERT INTO events(item_id,revision,action,actor,reason,payload,created_at) VALUES(?,?,?,?,?,?,?)', (item_id, revision + 1, action, actor.strip(), reason.strip(), canonical(value), now()))
        return {'id': item_id, 'revision': revision + 1, 'status': status}

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
            facts = []
            for row in db.execute("SELECT i.*,v.sha256 FROM items i JOIN documents d ON i.document_id=d.id AND i.version=d.current_version JOIN versions v ON v.document_id=i.document_id AND v.version=i.version WHERE i.kind='fact' AND i.status='approved' ORDER BY i.id"):
                facts.append(self.unpack(row))
            body = {'facts': facts, 'documents': [dict(r) for r in db.execute('SELECT id,current_version FROM documents ORDER BY id')]}
            key = digest(body)
            value = {'id': key, **body}
            db.execute('INSERT OR IGNORE INTO snapshots VALUES(?,?,?)', (key, canonical(value), now()))
            return value

    def save_query(self, result):
        key = ident()
        with self.connect(True) as db:
            db.execute('INSERT INTO queries VALUES(?,?,?)', (key, canonical(result), now()))
        return key

    def queries(self):
        with self.connect() as db:
            return [self.unpack(r) for r in db.execute('SELECT * FROM queries ORDER BY created_at DESC LIMIT 30')]
