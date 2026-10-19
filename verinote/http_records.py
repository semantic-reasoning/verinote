"""Single process-local writer for optional HTTP call telemetry.

One folder owns one provider call ID. run.json is authoritative for identity
and validation state; usage.json is accepted only with that same ID. Callers
supply trusted canonical folders, never paths from a public request.
"""
import json
import os
from pathlib import Path
import re
import stat
import threading
from uuid import uuid4

from .store import now
from .usage import MAX_METADATA, calculate, decode_http, empty_usage, safe_file

_LOCK = threading.RLock()
WARNING = 'HTTP 호출 기록 저장에 실패해 사용량 기록이 불완전합니다.'
VALIDATION = ('pending', 'success', 'failed', 'not_applicable')


class RecordConflict(ValueError):
    pass


def _directory(folder):
    """Pin every directory component with no-follow descriptors (POSIX)."""
    path = Path(folder).absolute()
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if part in ('.', '..'):
                raise OSError('unsafe directory')
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                os.mkdir(part, 0o700, dir_fd=fd)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def write_record(folder, filename, value, limit=MAX_METADATA):
    data = json.dumps(value, ensure_ascii=True, allow_nan=False, indent=2).encode()
    if len(data) > limit:
        raise OSError('record too large')
    fd = _directory(folder)
    temp = '.' + uuid4().hex + '.tmp'
    try:
        try:
            info = os.stat(filename, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise OSError('unsafe destination')
        except FileNotFoundError:
            pass
        out = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        with os.fdopen(out, 'wb') as stream:
            stream.write(data)
        os.replace(temp, filename, src_dir_fd=fd, dst_dir_fd=fd)
    finally:
        try:
            try:
                os.unlink(temp, dir_fd=fd)
            except FileNotFoundError:
                pass
        finally:
            os.close(fd)


def _read(folder, name):
    raw = safe_file(folder, name)
    try:
        value = json.loads(raw) if raw is not None else None
        return value if isinstance(value, dict) else None
    except (ValueError, UnicodeError, RecursionError):
        return None


def _valid_run(value):
    return (isinstance(value, dict) and value.get('transport') == 'http'
            and value.get('agent') == 'openrouter'
            and isinstance(value.get('provider_call_id'), str)
            and re.fullmatch(r'[a-f0-9]{32}', value['provider_call_id'])
            and value.get('caller_validation_status') in VALIDATION)


class HTTPRecord:
    def __init__(self, folder, call_id, *, model, phase, prompt_hash, started_at, options, rate, max_output):
        self.folder, self.call_id, self.max_output = Path(folder), call_id, max_output
        self.meta = {'agent': 'openrouter', 'transport': 'http', 'provider_call_id': call_id,
                     'requested_model': model, 'resolved_model': None, 'phase': phase,
                     'prompt_sha256': prompt_hash, 'started_at': started_at, 'finished_at': None,
                     'status': 'running', 'transport_status': 'running',
                     'transmission_state': 'not_sent', 'adapter_validation_status': 'pending',
                     'caller_validation_status': 'pending', 'dispatcher_finished': False, 'timeout_seconds': options.timeout,
                     'max_tokens': options.max_tokens, 'http_status': None, 'generation_id': None}
        self.usage = empty_usage('openrouter', model, rate)
        self.usage.update(provider_call_id=call_id, started_at=started_at, status='running')

    def _save(self, usage=True, payload=None):
        failed = False
        for filename, value, limit in [('run.json', self.meta, MAX_METADATA),
                                        *([('usage.json', self.usage, MAX_METADATA)] if usage else []),
                                        *([('response.json', payload, self.max_output * 6 + 1024)] if payload is not None else [])]:
            try:
                write_record(self.folder, filename, value, limit)
            except (OSError, ValueError, TypeError, RecursionError):
                failed = True
        if failed:
            self.meta['usage_warning'] = WARNING
            try:
                write_record(self.folder, 'run.json', self.meta)
            except (OSError, ValueError, TypeError, RecursionError):
                pass
        return not failed

    def begin(self):
        with _LOCK:
            # Existing or unreadable records are not evidence of a fresh call.
            for name in ('run.json', 'usage.json', 'response.json'):
                if (self.folder / name).exists() or (self.folder / name).is_symlink():
                    raise RecordConflict('HTTP 호출 폴더에 이미 실행 기록이 있습니다.')
            self._save()
        return self

    def consume(self, envelope):
        with _LOCK:
            if envelope.call_id != self.call_id:
                raise RecordConflict('HTTP 호출 ID가 일치하지 않습니다.')
            meta = envelope.safe_metadata
            validation = meta.get('validation_status')
            self.meta.update(transport_status=envelope.transport_status,
                             adapter_validation_status=validation if validation in ('valid', 'failed', 'unverified') else 'pending',
                             transmission_state=meta.get('transmission_state', 'received' if envelope.response_verified else 'sent_unknown'),
                             finished_at=now())
            for field in ('resolved_model', 'http_status', 'generation_id', 'finish_reason'):
                self.meta[field] = meta.get(field) if envelope.response_verified or field == 'http_status' else None
            self.meta['status'] = 'success' if envelope.transport_status == 'success' and validation != 'failed' else 'not_started' if envelope.transport_status == 'not_started' else 'error'
            if self.meta['status'] != 'success':
                self.meta['caller_validation_status'] = 'not_applicable'
            parsed = decode_http(envelope)
            self.usage.update({k: v for k, v in parsed.items() if k not in ('requested_model', 'rate_snapshot', 'schema_version')})
            self.usage.update(status=self.meta['status'], started_at=self.meta['started_at'], finished_at=self.meta['finished_at'])
            calculate(self.usage)
            self._save(payload=envelope.safe_response_payload if envelope.response_verified else None)

    def adapter_finished(self, success):
        with _LOCK:
            self.meta['dispatcher_finished'] = True
            self.meta['adapter_validation_status'] = 'valid' if success else 'failed'
            if not success:
                self.meta.update(status='error', caller_validation_status='not_applicable')
                self.usage['status'] = 'error'
            self._save()

    def no_envelope(self):
        with _LOCK:
            self.meta.update(status='error', dispatcher_finished=True, transport_status='error', transmission_state='sent_unknown',
                             adapter_validation_status='unverified', caller_validation_status='not_applicable', finished_at=now())
            self.usage.update(status='error', finished_at=self.meta['finished_at'])
            self._save()


def update_caller_validation(folder, call_id, status):
    """#707 internal API; no free-form text. Reconstruct from authoritative run.

    Returns False for unknown/stale/unreadable calls or optional write failure.
    Repeating a terminal status is idempotent; conflicting terminal values raise.
    The lock covers all read/compare/write operations within this process.
    """
    if status not in ('success', 'failed'):
        raise ValueError('알 수 없는 호출자 검증 상태')
    with _LOCK:
        run = _read(folder, 'run.json')
        if not _valid_run(run) or run['provider_call_id'] != call_id or run.get('adapter_validation_status') != 'valid' or run.get('dispatcher_finished') is not True:
            return False
        current = run['caller_validation_status']
        if current == status:
            return True
        if current != 'pending':
            raise RecordConflict('호출자 검증의 최종 상태는 변경할 수 없습니다.')
        run.update(caller_validation_status=status, status='success' if status == 'success' else 'error')
        try:
            write_record(folder, 'run.json', run)
        except (OSError, ValueError, TypeError, RecursionError):
            return False
        return True
