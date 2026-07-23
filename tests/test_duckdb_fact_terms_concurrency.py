# SPDX-License-Identifier: MPL-2.0
"""Cross-process concurrency tests for the DuckDB fact-term sidecar (#169).

DuckDB takes a single-process write lock, so the interesting failure only shows
up across a real OS process boundary: two connections to the same file from
*within one process* share DuckDB's per-process instance cache and never
conflict, which would make a same-process test a false green. Every test here
that exercises lock contention therefore spawns a genuine child process.
"""

import select
import subprocess
import sys
import time

import pytest

from verinote import cli
from verinote.config import local_root
from verinote.engine.terms import StringLit
from verinote.store import duckdb_fact_terms
from verinote.store.duckdb_fact_terms import (
    DuckDBFactTermStore,
    DuckDBFactTermStoreError,
    DuckDBFactTermStoreLockedError,
    FACT_TERMS_FILENAME,
)

# The subprocess tests read child output through `select`, which does not work on
# Windows pipes; Windows also has different file-locking semantics. The fix is
# cross-platform, but reliably *exercising* the lock across processes is a POSIX
# affair here. The two unit tests below (in-memory regression, non-lock
# classification) carry no such dependency and run everywhere.
_posix_only = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="cross-process file-lock contention needs POSIX select/lock semantics",
)


def _duckdb():
    return pytest.importorskip("duckdb")


# A raw DuckDB connection held open in another process -- exactly what an external
# process (or, before this fix, a `verinote ui` fact-page render) does to the
# sidecar. Post-fix the store holds no connection between calls, so to *hold* the
# lock a test needs a genuine adversary like this, not a `DuckDBFactTermStore`.
_HOLDER = """
import sys, time, duckdb
path, max_hold = sys.argv[1], float(sys.argv[2])
con = duckdb.connect(path)
con.execute(
    "CREATE TABLE IF NOT EXISTS fact_terms ("
    "fact_id BIGINT PRIMARY KEY, subject VARCHAR NOT NULL, "
    "rel VARCHAR NOT NULL, object VARCHAR NOT NULL, term_token VARCHAR)"
)
sys.stdout.write("READY\\n")
sys.stdout.flush()
time.sleep(max_hold)
con.close()
"""

# The post-fix web UI: opens the sidecar per operation, reads, and lets go --
# repeatedly, mimicking a user paging through fact views while a sync runs.
_UI_LOOP = """
import sys, time
from verinote.store.duckdb_fact_terms import (
    DuckDBFactTermStore,
    DuckDBFactTermStoreLockedError,
)
root, duration = sys.argv[1], float(sys.argv[2])
store = DuckDBFactTermStore.for_root(root)
deadline = time.monotonic() + duration
reads = 0
announced = False
try:
    while time.monotonic() < deadline:
        store.get_many_fact_terms([1, 2, 3, 4, 5])
        reads += 1
        if not announced:
            sys.stdout.write("READY\\n")
            sys.stdout.flush()
            announced = True
    sys.stdout.write("OK reads=%d\\n" % reads)
    sys.stdout.flush()
except DuckDBFactTermStoreLockedError as exc:
    sys.stdout.write("LOCKED reads=%d %s\\n" % (reads, exc))
    sys.stdout.flush()
    sys.exit(3)
"""

# A second verinote process opening the sidecar with NO patience of its own: a
# near-zero retry budget. It succeeds only if the lock is free the instant it
# tries -- so it cannot be carried by retry-wait masking a still-held connection.
_OPEN_PROBE = """
import sys
from verinote.store import duckdb_fact_terms
from verinote.store.duckdb_fact_terms import (
    DuckDBFactTermStore,
    DuckDBFactTermStoreLockedError,
)
root, budget = sys.argv[1], float(sys.argv[2])
duckdb_fact_terms._LOCK_TIMEOUT_SECONDS = budget
store = DuckDBFactTermStore.for_root(root)
try:
    store.get_fact_terms(1)
    sys.stdout.write("OK\\n")
    sys.stdout.flush()
except DuckDBFactTermStoreLockedError as exc:
    sys.stdout.write("LOCKED %s\\n" % exc)
    sys.stdout.flush()
    sys.exit(4)
"""


def _spawn(tmp_path, source, *args):
    script = tmp_path / "child.py"
    script.write_text(source)
    return subprocess.Popen(
        [sys.executable, str(script), *[str(a) for a in args]],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        # A neutral cwd: never the repo root, so the child imports the installed
        # package rather than shadowing it via sys.path[0].
        cwd=str(tmp_path),
    )


def _readline_until(proc, token, timeout):
    """Return the first child stdout line containing `token`, or None on timeout/EOF."""
    end = time.monotonic() + timeout
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return None
        ready, _, _ = select.select([proc.stdout], [], [], remaining)
        if not ready:
            return None
        line = proc.stdout.readline()
        if line == "":
            return None
        if token in line:
            return line


def _terminate(proc):
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


@_posix_only
def test_open_raises_locked_error_when_another_process_holds_it(tmp_path, monkeypatch):
    _duckdb()
    monkeypatch.setattr(duckdb_fact_terms, "_LOCK_TIMEOUT_SECONDS", 0.0)
    root = tmp_path / "kb"
    root.mkdir()
    sidecar = root / FACT_TERMS_FILENAME
    holder = _spawn(tmp_path, _HOLDER, sidecar, 30)
    try:
        assert _readline_until(holder, "READY", 20) is not None, "holder never took the lock"
        store = DuckDBFactTermStore.for_root(root)
        with pytest.raises(DuckDBFactTermStoreLockedError):
            store.get_fact_terms(1)
    finally:
        _terminate(holder)


@_posix_only
def test_retry_succeeds_when_holder_releases_within_budget(tmp_path):
    _duckdb()
    root = tmp_path / "kb"
    root.mkdir()
    sidecar = root / FACT_TERMS_FILENAME
    # Holder auto-releases after 0.5s, well inside the default ~5s retry budget.
    holder = _spawn(tmp_path, _HOLDER, sidecar, 0.5)
    try:
        assert _readline_until(holder, "READY", 20) is not None, "holder never took the lock"
        store = DuckDBFactTermStore.for_root(root)
        # Starts while the lock is held; the retry loop waits it out and then the
        # read finds an empty table -- no lock error escapes.
        assert store.get_fact_terms(1) is None
    finally:
        _terminate(holder)


@_posix_only
def test_cli_seed_floors_lock_conflict_without_traceback(tmp_path, monkeypatch, capsys):
    _duckdb()
    root = local_root(tmp_path / "kb")
    assert cli.main(["init", str(root)]) == 0
    capsys.readouterr()
    sidecar = root / FACT_TERMS_FILENAME
    holder = _spawn(tmp_path, _HOLDER, sidecar, 30)
    try:
        assert _readline_until(holder, "READY", 20) is not None, "holder never took the lock"
        monkeypatch.setattr(duckdb_fact_terms, "_LOCK_TIMEOUT_SECONDS", 0.0)
        rc = cli.main(["seed", str(root)])
        captured = capsys.readouterr()
    finally:
        _terminate(holder)

    combined = captured.out + captured.err
    assert rc == 1
    # Actionable, and points at the real cause and the way out.
    assert "lock" in combined.lower()
    assert "verinote ui" in combined
    assert FACT_TERMS_FILENAME in combined
    # No raw traceback or exception-class name leaks to the user.
    assert "Traceback" not in combined
    assert "DuckDBFactTermStoreLockedError" not in combined


@_posix_only
def test_ui_reads_and_cli_seed_run_concurrently_without_lock_failures(tmp_path, capsys):
    _duckdb()
    root = local_root(tmp_path / "kb")
    assert cli.main(["init", str(root)]) == 0
    capsys.readouterr()
    # Establish the sidecar file + schema up front so the concurrent run exercises
    # pure read/write lock contention, not a first-writer file-creation race.
    DuckDBFactTermStore.for_root(root).get_many_fact_terms([1])

    ui = _spawn(tmp_path, _UI_LOOP, root, 2.0)
    try:
        assert _readline_until(ui, "READY", 20) is not None, "UI read loop never started"
        # A real CLI write command running while the UI reads the same sidecar.
        assert cli.main(["seed", str(root)]) == 0
        capsys.readouterr()
        rest = ui.communicate(timeout=30)[0]
    except subprocess.TimeoutExpired:
        _terminate(ui)
        raise
    assert ui.returncode == 0, f"UI read loop failed: {rest!r}"
    assert "LOCKED" not in rest
    assert "OK reads=" in rest


@_posix_only
def test_operation_releases_the_lock_as_soon_as_it_returns(tmp_path):
    _duckdb()
    # Isolates the core #169 mechanism -- releasing the connection when an
    # operation returns -- from the retry logic that can otherwise mask a still-held
    # connection. A file-backed store does exactly ONE operation and returns; under
    # the fix its `_operation()` finally-closed the connection and dropped the OS
    # lock. A separate process then opens the same sidecar with a near-ZERO retry
    # budget of its own, so it can only succeed if the lock is genuinely free right
    # now -- retry cannot carry it. If the store instead held the connection open
    # for its lifetime (the literal #169 bug), this probe would hit the lock and,
    # with no budget to wait it out, fail. The store is kept alive across the probe
    # so that a lifetime-held connection would still be holding here.
    root = tmp_path / "kb"
    root.mkdir()
    store = DuckDBFactTermStore.for_root(root)
    try:
        store.put_fact_terms(1, "A", "r", "B")
        probe = _spawn(tmp_path, _OPEN_PROBE, root, 0.0)
        try:
            out = probe.communicate(timeout=30)[0]
        except subprocess.TimeoutExpired:
            _terminate(probe)
            raise
        assert probe.returncode == 0, (
            f"the sidecar lock was still held after the operation returned: {out!r}"
        )
        assert "OK" in out
    finally:
        store.close()


def test_in_memory_store_is_unchanged_by_the_per_operation_fix(tmp_path):
    _duckdb()
    # The in-memory store still holds one connection for its whole life: a write
    # and a later read are separate method calls, and the row must survive between
    # them (a fresh :memory: connection would be an empty database).
    store = DuckDBFactTermStore(None)
    try:
        store.put_fact_terms(1, "A", "r", "B")
        assert store.get_fact_terms(1) == (StringLit("A"), StringLit("r"), StringLit("B"))
    finally:
        store.close()

    # And operating on a closed in-memory store still raises, unchanged.
    store.close()
    with pytest.raises(DuckDBFactTermStoreError, match="closed"):
        store.get_fact_terms(1)


def test_non_lock_open_failure_stays_generic_not_locked(tmp_path):
    _duckdb()
    root = tmp_path / "kb"
    root.mkdir()
    # A file that is not a DuckDB database: opening it fails, but not with a lock
    # conflict. The retry/timeout path must not misclassify it as locked.
    (root / FACT_TERMS_FILENAME).write_bytes(b"not a duckdb database file" * 8)
    store = DuckDBFactTermStore.for_root(root)
    with pytest.raises(DuckDBFactTermStoreError) as excinfo:
        store.get_fact_terms(1)
    assert not isinstance(excinfo.value, DuckDBFactTermStoreLockedError)
