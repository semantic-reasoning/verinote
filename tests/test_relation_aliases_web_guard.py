# SPDX-License-Identifier: MPL-2.0
r"""A broken `policy/relation-aliases.md` degrades `/`, `/sources`, `/settings`
instead of 500ing them (#555).

WHY THE MALFORMED TESTS ASSERT THE MESSAGE, NOT THE STATUS. Deleting the narrow
`except CorroborationPolicyError` clause (G1) inside `_relation_alias_failure`
does NOT turn any route back into a 500: the malformed case falls through to the
broad `except Exception` clause (G2) underneath, which also returns a string, so
every affected route still renders 200 (measured — see plan555.md §2.2/M9,
critique555.md's independent reproduction). What changes is the MESSAGE: the
banner goes from `relation-aliases.md:1: expected \`raw\` -> \`canonical\`` to
`policy/relation-aliases.md could not be read: relation-aliases.md:1: expected …`
— the file's own name, prefixed onto a message that already carries the file's
own name, claiming a file that WAS read and DID parse "could not be read" at
all. That is a false statement about the system's own state on a user-facing
page, not a cosmetic duplication, and this repo gates on state-honesty. A test
written as `assert response.status_code == 200` does not detect it. So every
malformed-input test below asserts the body contains the bare parser message
(`PARSER_MSG`) and does NOT contain the "could not be read" wrapper (`NAMED`).
Do not shorten these to a status-code check — that silently un-pins G1.

WHY THE EMPTY-SAVE REFUSAL KEYS ON A HIDDEN FIELD, NOT A RE-READ AT SUBMIT TIME.
`/settings/relation-aliases` refuses an empty submit while the box was rendered
empty because the on-disk file could not be read (BLOCKER-2/BLOCKER-3 in the
#555 fix-round gate) — otherwise `relation_aliases("")` parses cleanly and the
submit reaches `path.unlink()`, deleting the user's only copy. An earlier
version of this refusal re-derived "is the file unreadable?" at submit time,
which raced: a stale tab that rendered while the file was broken could submit
its empty box *after* someone repaired the file on disk, the re-derived check
would see a now-readable file, not refuse, and delete the just-repaired file.
The fix carries a hidden `relation_aliases_rendered_unreadable` field that the
template only emits when THAT render's box was empty for that reason, and the
route refuses on the hidden field rather than re-reading. Two tests pin both
directions: `test_settings_refuses_a_stale_empty_submit_even_after_the_file_is_repaired`
(the race) and `test_settings_lets_a_genuine_empty_submit_delete_a_healthy_file`
(the feature the refusal must not break).

WHY THE SAVE WRITES THROUGH A TEMP FILE (`_write_relation_aliases_atomic`),
NOT `Path.write_text` (#555 gate REV-3). `write_text` truncates on open, so a
write that fails partway (a full disk, a killed process, ...) can leave the
alias file empty or half-written while the page still says "Nothing was
saved" -- measured end-to-end on a real full filesystem: the file was left
`b""` and the claim was false. `_write_relation_aliases_atomic` writes a
sibling temp file and `os.replace`s it in, so any failure leaves the original
file untouched; `test_settings_save_leaves_the_file_untouched_when_the_write_fails`
pins that with a monkeypatched `fsync` failure (a POST cannot deliver a body
that fails to write for an encoding reason, so this is how the real trigger --
ENOSPC -- actually reaches the code). A second, non-obvious consequence of
`os.replace`-based writes is that they succeed against a file this process
cannot itself read or write (`chmod 000`), because replacing a directory
entry needs write access to the DIRECTORY, not to the file being replaced --
`test_settings_can_repair_a_permission_denied_alias_file` pins that the
remedy this page prescribes for an unreadable file actually works, and that
the new file's mode does not simply repeat the broken one it replaced.

The write/delete guard clause itself carries three more pins (#555 gate
rev-2): the failure message names the operation that actually ran ("written"
vs "removed" -- `test_settings_save_names_removed_not_written_when_the_delete_fails`),
the empty-submit refusal arms on ANY non-empty hidden-field value rather than
one exact string (`test_settings_refusal_arms_on_any_non_empty_hidden_field_value`),
and the clause is `except Exception`, matching this file's own house form at
`save_prompt_route`/`reset_prompt_route`, not `except OSError`
(`test_settings_save_catches_a_non_oserror_write_failure`).

SCOPE. Only the three routes issue #555 names: `/`, `/sources`, `/settings`.
`/review` and `/workbench` have the same defect (and `/review` has a second,
independent alias-read site the naive fix misses — see plan555.md M10 and
critique555.md BLOCK-1, which also found `POST /facts/{id}/{accept,reject,toggle}`
and `GET /facts/{id}/{row,provenance}` broken the same way) but are deliberately
NOT touched here; they are registered as a follow-up issue instead of being
silently dropped. `store_typed_relations` (`policy/typed-relations.md`) is also
untouched — `_source_trust_rollup` still calls it alongside
`store_relation_aliases`, so `/sources` can still 500 on a broken
`typed-relations.md` after this change. The dashboard's degraded queue rows
still link to `/review` and `/workbench` for their OTHER (non-degraded) rows,
and those two routes can still 500 under a broken alias file (#570, also out of
scope) -- `test_dashboard_offers_no_open_button_into_a_not_computed_queue_row`
pins that the degraded rows themselves do not offer that button.
"""

import stat
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from verinote.config import Config
from verinote.policy_defaults import RELATION_ALIASES_RELPATH
from verinote.web import create_app

PARSER_MSG = "relation-aliases.md:1:"
NAMED = f"{RELATION_ALIASES_RELPATH} could not be read"

MALFORMED_BYTES = "- 소속 member_of\n".encode()  # valid UTF-8, arrow missing
CP949_BYTES = "- 소속 -> member_of\n".encode("cp949")
HEALTHY_BYTES = "- 소속 -> member_of\n".encode()

RENDERED_UNREADABLE_FIELD = (
    '<input type="hidden" name="relation_aliases_rendered_unreadable" value="1">'
)


def _make_kb(tmp_path: Path) -> Path:
    root = tmp_path / "kb"
    root.mkdir()
    return root


def _open_app(root: Path):
    cfg = Config(
        root=root,
        db_path=root / "kb.sqlite",
        provider="anthropic",
        model="m",
        api_key=None,
        base_url=None,
    )
    app = create_app(cfg)
    store = app.state.store
    source_id = store.add_source("sources/a.txt")
    store.add_fact(
        "A", "소속", "B", status="confirmed", confidence=0.9, source_id=source_id
    )
    store.add_fact(
        "C", "소속", "D", status="needs_review", confidence=0.5, source_id=source_id
    )
    return app


def _client(tmp_path: Path, alias_bytes: bytes | None) -> TestClient:
    """A KB with one confirmed, source-backed `소속` fact and one `needs_review`
    fact, so the alias-dependent numbers (corroboration, review-queue trust
    labels) are non-trivial rather than vacuously empty on a healthy alias file.
    """
    root = _make_kb(tmp_path)
    app = _open_app(root)
    if alias_bytes is not None:
        path = root / RELATION_ALIASES_RELPATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(alias_bytes)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def malformed_client(tmp_path: Path) -> TestClient:
    return _client(tmp_path, MALFORMED_BYTES)


@pytest.fixture
def cp949_client(tmp_path: Path) -> TestClient:
    return _client(tmp_path, CP949_BYTES)


@pytest.fixture
def healthy_client(tmp_path: Path) -> TestClient:
    return _client(tmp_path, HEALTHY_BYTES)


@pytest.fixture
def absent_client(tmp_path: Path) -> TestClient:
    return _client(tmp_path, None)


# ---------------------------------------------------------------------------
# Unit A -- /sources
# ---------------------------------------------------------------------------


def test_sources_survives_a_malformed_alias_file_and_keeps_the_parser_message(
    malformed_client,
):
    r = malformed_client.get("/sources")
    assert r.status_code == 200
    assert PARSER_MSG in r.text
    assert NAMED not in r.text


def test_sources_survives_a_cp949_alias_file_and_names_the_file(cp949_client):
    r = cp949_client.get("/sources")
    assert r.status_code == 200
    assert NAMED in r.text


def test_sources_shows_no_trust_counts_it_could_not_compute(malformed_client):
    """Refuses to fall back to defaults/`{}` -- see plan555.md Q3.

    Asserted on the trust block and the exact label, not the bare words
    unsupported/conflicted/corroborated: `sources.html` also renders
    `<span class="badge trust-conflicted">chunk N</span>` for a failed
    extraction chunk, which is unrelated to trust COUNTS (critique555.md N-3).
    """
    r = malformed_client.get("/sources")
    assert '<span class="badge muted">trust not computed</span>' in r.text
    assert 'unsupported</span>' not in r.text
    assert 'conflicted</span>' not in r.text
    assert 'corroborated</span>' not in r.text


def test_a_healthy_alias_file_still_shows_the_sources_trust_counts(healthy_client):
    """Anti-vacuity control for the three tests above: a guard that fires
    unconditionally would also pass them."""
    r = healthy_client.get("/sources")
    assert r.status_code == 200
    assert "trust not computed" not in r.text
    assert PARSER_MSG not in r.text
    assert NAMED not in r.text
    assert 'unsupported</span>' in r.text


# ---------------------------------------------------------------------------
# Unit B -- /
# ---------------------------------------------------------------------------


def test_dashboard_survives_a_malformed_alias_file_and_keeps_the_parser_message(
    malformed_client,
):
    r = malformed_client.get("/")
    assert r.status_code == 200
    assert PARSER_MSG in r.text
    assert NAMED not in r.text


def test_dashboard_survives_a_cp949_alias_file_and_names_the_file(cp949_client):
    r = cp949_client.get("/")
    assert r.status_code == 200
    assert NAMED in r.text


def test_dashboard_does_not_claim_there_are_no_corroborated_facts(malformed_client):
    """`[]` and `None` both render falsy in `{% if %}`, but only `None` lets the
    template tell "not computed" apart from "computed, and empty" -- passing `[]`
    would print the exact same "No source-backed …" prose a healthy, empty KB
    gets, which is a false statement about a KB that was never analysed."""
    r = malformed_client.get("/")
    assert "No source-backed engine-input facts yet." not in r.text
    assert "No source-backed single-valued conflicts." not in r.text
    assert r.text.count("Not computed — see the alias-file notice above.") == 2
    # The four alias-dependent queue rows show the not-computed marker, not a
    # digit -- plus one more occurrence inside the banner's own sentence, which
    # names the marker it is pointing at (MUST-FIX-3, #555 fix-round gate).
    assert r.text.count('<span class="badge muted">not computed</span>') == 5


def test_a_healthy_alias_file_still_shows_the_dashboard_corroboration_table(
    healthy_client,
):
    r = healthy_client.get("/")
    assert r.status_code == 200
    assert "Not computed — see the alias-file notice above." not in r.text
    assert '<span class="badge muted">not computed</span>' not in r.text
    # The healthy KB's one confirmed, source-backed fact appears in the table.
    assert "<code>A</code>" in r.text


def test_dashboard_offers_no_open_button_into_a_not_computed_queue_row(
    malformed_client,
):
    """MUST-FIX-2 (#555 fix-round gate). Before this, a degraded row still
    carried a live `Open` button into `/review?filter=…` or `/workbench`, which
    are measured to 500 under this same broken alias file (#570). The banner
    explains the degradation; a live button into a crash from that same page is
    the "looks healthy, isn't" shape criterion 3 exists to prevent."""
    r = malformed_client.get("/")
    for href in ("/review?filter=unsupported", "/review?filter=corroborated", "/workbench"):
        assert f'<a class="btn ghost" href="{href}">Open</a>' not in r.text
    # The two alias-INDEPENDENT rows ("Failed source analyses" -> /sources,
    # "Recent lifecycle changes" -> /review) show real counts and keep their
    # buttons -- this is not a blanket "hide every Open button" change.
    assert '<a class="btn ghost" href="/sources">Open</a>' in r.text
    assert '<a class="btn ghost" href="/review">Open</a>' in r.text


def test_a_healthy_alias_file_still_offers_every_open_button(healthy_client):
    """Anti-vacuity control: a template that hid Open buttons unconditionally
    would also pass the test above."""
    r = healthy_client.get("/")
    assert r.text.count("Open</a>") == 6


# ---------------------------------------------------------------------------
# Unit C -- /settings
# ---------------------------------------------------------------------------


def test_settings_names_the_parse_error_in_a_malformed_alias_file(malformed_client):
    """Red on the parent commit too: today `/settings` is 200 and silent on this
    same input (plan555.md M3) -- that silence is what this test is closing."""
    r = malformed_client.get("/settings")
    assert r.status_code == 200
    assert PARSER_MSG in r.text


def test_settings_survives_a_cp949_alias_file_and_names_the_file(cp949_client):
    """Red on the parent commit: `/settings` 500s on cp949 today (M1/M2)."""
    r = cp949_client.get("/settings")
    assert r.status_code == 200
    assert NAMED in r.text


def test_settings_says_the_empty_alias_box_is_not_the_file_on_disk(cp949_client):
    r = cp949_client.get("/settings")
    assert r.status_code == 200
    # The textarea is empty -- nothing decoded from the file was put into it.
    assert '<textarea name="relation_aliases_text" rows="8"\n              placeholder="- `역할` -> `role`"></textarea>' in r.text
    # BLOCKER-3: the broad clause behind this banner also catches non-decode
    # failures (permission errors, …), so the copy says "read", never "decoded"
    # -- "decoded" was only ever true for this one mode among several.
    assert "could not be read" in r.text
    assert "decoded" not in r.text
    # The refusal exists, and the page says so, rather than only predicting a
    # deletion that a later POST in fact refuses (BLOCKER-1).
    assert "refused" in r.text.lower()
    # The render carries the hidden marker the POST refusal keys on.
    assert RENDERED_UNREADABLE_FIELD in r.text


def test_settings_refuses_a_stale_empty_submit_even_after_the_file_is_repaired(
    cp949_client, tmp_path
):
    """MUST-FIX-1 (#555 fix-round gate). The refusal must be attributable to
    what THIS render showed, not re-derived from the file's state at submit
    time. Measured end-to-end against the earlier (re-deriving) shape: render
    while the file is unreadable (empty box, hidden field set) -> repair the
    file on disk, as the page's own advice says to -> submit the STALE empty
    box -> the re-deriving shape saw a now-readable file, did not refuse, and
    deleted the just-repaired file. This test pins that the repaired file
    survives a submit carrying the render-time hidden marker regardless of the
    file's state when the submit arrives."""
    root = tmp_path / "kb"
    alias_path = root / RELATION_ALIASES_RELPATH

    r = cp949_client.get("/settings")
    assert RENDERED_UNREADABLE_FIELD in r.text  # this render's box was empty

    repaired = "- 소속 -> member_of\n".encode()  # valid UTF-8, healthy
    alias_path.write_bytes(repaired)

    r2 = cp949_client.post(
        "/settings/relation-aliases",
        data={
            "relation_aliases_text": "",
            "relation_aliases_rendered_unreadable": "1",
        },
    )
    assert r2.status_code == 400
    assert "nothing was saved" in r2.text.lower()
    assert alias_path.read_bytes() == repaired


def test_settings_lets_a_genuine_empty_submit_delete_a_healthy_file(
    healthy_client, tmp_path
):
    """BLOCKER-2 (#555 fix-round gate): the narrow side of the refusal. Without
    a test posting an empty body against a HEALTHY file, a mutant that refuses
    every empty submit unconditionally (destroying the deliberate-clear
    feature) survives the whole suite. A render of a readable, non-empty file
    never emits the hidden marker (see `test_settings_says_the_empty_alias_box…`
    for the case that does), so a genuine empty submit from that render carries
    no `relation_aliases_rendered_unreadable` field and must still delete the
    file -- this mirrors the ORIGINAL, pre-#555 behaviour for that state."""
    root = tmp_path / "kb"
    alias_path = root / RELATION_ALIASES_RELPATH
    assert alias_path.exists()

    r = healthy_client.get("/settings")
    assert RENDERED_UNREADABLE_FIELD not in r.text  # this render's box was NOT empty

    r2 = healthy_client.post(
        "/settings/relation-aliases",
        data={"relation_aliases_text": ""},
        follow_redirects=False,
    )
    assert r2.status_code == 303
    assert not alias_path.exists()


def test_settings_names_a_permission_failure_as_unreadable_not_undecoded(tmp_path):
    """BLOCKER-3 half 1 (#555 fix-round gate). `relation_aliases_unreadable`
    comes from a BROAD `except Exception` around `read_text`, so it is also
    true for `PermissionError` -- a `chmod 000` file, not a decode failure.
    The banner and warning must say "could not be read", never "decoded",
    for both halves of what that broad clause catches."""
    root = _make_kb(tmp_path)
    app = _open_app(root)
    client = TestClient(app, raise_server_exceptions=False)
    path = root / RELATION_ALIASES_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("- 소속 -> member_of\n", encoding="utf-8")
    path.chmod(0o000)
    try:
        try:
            path.read_text(encoding="utf-8")
        except PermissionError:
            pass
        else:
            pytest.skip("this user reads straight through mode 0o000")

        r = client.get("/settings")
        assert r.status_code == 200
        assert "could not be read" in r.text
        assert "decoded" not in r.text
    finally:
        path.chmod(0o600)
        path.unlink(missing_ok=True)


def test_settings_can_repair_a_permission_denied_alias_file(tmp_path):
    """The remedy for a `chmod 000` alias file actually works, and leaves the
    result readable. Measured (#555 REV-3, mode-preservation follow-up): the
    FIRST atomic-write shape tried here derived the new file's mode from the
    file it was replacing, so "repairing" a `chmod 000` file wrote valid
    content into a NEW file that was ALSO `chmod 000` -- the POST 303'd as
    though it worked, but the very next `GET /settings` showed "could not be
    read" again, unreadable by the same mode it had before. The fix is to use
    a fixed `0o644` rather than deriving one from the existing file (see
    `_write_relation_aliases_atomic`'s docstring for why fixed, and what it
    costs -- `write_text` PRESERVES an existing file's mode, so this is a
    deliberate difference from it, not a match).

    This also confirms `os.replace` genuinely repairs a `chmod 000` file:
    replacing a directory entry needs write access to the DIRECTORY, not to
    the file being replaced, so `_write_relation_aliases_atomic` can succeed
    here even though a direct `open(path, 'w')` (the prior `write_text`
    implementation) could not have.
    """
    root = _make_kb(tmp_path)
    app = _open_app(root)
    client = TestClient(app, raise_server_exceptions=False)
    path = root / RELATION_ALIASES_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("- 소속 -> member_of\n", encoding="utf-8")
    path.chmod(0o000)
    try:
        try:
            path.read_text(encoding="utf-8")
        except PermissionError:
            pass
        else:
            pytest.skip("this user reads straight through mode 0o000")

        r2 = client.post(
            "/settings/relation-aliases",
            data={"relation_aliases_text": "- 소속 -> role"},
            follow_redirects=False,
        )
        assert r2.status_code == 303

        r3 = client.get("/settings")
        assert r3.status_code == 200
        assert "could not be read" not in r3.text
        assert "role" in r3.text
    finally:
        path.chmod(0o600)
        path.unlink(missing_ok=True)


def test_settings_save_leaves_the_file_untouched_when_the_write_fails(
    healthy_client, tmp_path, monkeypatch
):
    """BLOCKER-3 part 2 (#555 fix-round gate, REV-3). `write_text` truncates
    in place on open, so a write that fails partway through -- a full disk,
    a killed process, two processes saving at once -- could leave the file
    empty or half-written while the page claimed "Nothing was saved", which
    was then false. Measured end-to-end on a real full filesystem by the
    gate. The fix (`_write_relation_aliases_atomic`) writes a sibling temp
    file and `os.replace`s it in, so a failure anywhere in that sequence must
    leave the ORIGINAL file byte-for-byte untouched, with no temp file left
    behind.

    A POST cannot deliver text that fails to write for a form-encoding
    reason -- by the time this route sees it, it is already decoded text --
    so this drives the failure the way ENOSPC actually would reach the code:
    an `OSError` out of the `fsync` call, via monkeypatch, rather than
    through form input.

    NOT a durability test (#555 gate rev-2, Critic N11): patching `fsync` is a
    convenient injection POINT for a mid-sequence failure, not a claim that
    this test verifies data reaches disk. Whether `fsync` itself actually
    makes the write durable against a real power loss is not something a unit
    test can observe; what this test verifies is the code's reaction to A
    failure at that point in the sequence -- that the ORIGINAL file survives
    untouched -- which holds regardless of which step in the sequence raises.
    """
    root = tmp_path / "kb"
    alias_path = root / RELATION_ALIASES_RELPATH
    before = alias_path.read_bytes()

    def boom(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("verinote.web.app.os.fsync", boom)

    r = healthy_client.post(
        "/settings/relation-aliases",
        data={"relation_aliases_text": "- 소속 -> role"},
    )
    # 500, not 400 (#555 gate rev-7): `relation_aliases(text)` already
    # succeeded before this failure, so the request was valid -- the disk is
    # what said no. Matches `tests/test_web.py`'s pin for the identical
    # failure class on `save_prompt_route`/`reset_prompt_route`.
    assert r.status_code == 500
    assert "could not be written" in r.text
    assert "nothing was saved" in r.text.lower()
    assert alias_path.read_bytes() == before
    leftovers = [p for p in alias_path.parent.iterdir() if p != alias_path]
    assert leftovers == []


def test_settings_save_always_writes_mode_0o644(healthy_client, tmp_path):
    """#555 REV-4. Nothing pinned the resulting file's mode before this test
    -- three mutations (`mode = 0o600`, `mode = 0o666`, deleting the
    `fchmod`/`chmod` step entirely) all left the whole suite green.

    `write_text` PRESERVES an existing file's mode (measured independently: a
    `chmod 0o600` file stays `0o600` after `write_text` rewrites it), so a
    hand-written alias file under a tight umask would otherwise keep that
    mode forever, unaffected by any UI save. `_write_relation_aliases_atomic`
    deliberately applies a FIXED `0o644` instead (see its docstring) -- on
    existing files too, which is a real widening a security-conscious user
    would not expect from a "save" button.

    Start the file at `0o600` -- the documented hand-edit workflow under
    `umask 077` (`docs/operations.md:21` says the file is written "by hand or
    via the Settings UI") -- and confirm the save widens it to `0o644` rather
    than leaving `0o600` unpinned and silently correct only by accident.
    """
    root = tmp_path / "kb"
    alias_path = root / RELATION_ALIASES_RELPATH
    alias_path.chmod(0o600)

    r = healthy_client.post(
        "/settings/relation-aliases",
        data={"relation_aliases_text": "- 소속 -> role"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert stat.S_IMODE(alias_path.stat().st_mode) == 0o644


def test_settings_save_names_removed_not_written_when_the_delete_fails(tmp_path):
    """#555 gate rev-2 (Critic). The `elif path.exists(): path.unlink()` branch
    is the DELETE path -- reached on an empty, non-refused submit -- and its
    failure message must name the operation that actually ran. Measured before
    this fix: a directory sitting at the alias path made `unlink()` raise
    `PermissionError`/`IsADirectoryError` (platform-dependent), and the shared
    error message said "could not be WRITTEN", naming an operation (writing)
    that never happened -- the same class of defect as the "decoded" vs "read"
    mismatch fixed earlier in this file."""
    root = _make_kb(tmp_path)
    app = _open_app(root)
    client = TestClient(app, raise_server_exceptions=False)
    path = root / RELATION_ALIASES_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir()  # a directory, not a file, sits where the alias file goes

    r = client.post("/settings/relation-aliases", data={"relation_aliases_text": ""})
    # 500, not 400 (#555 gate rev-7): the empty submit itself was not refused
    # (the file is unreadable-as-a-directory, not "unreadable" in the sense
    # the hidden-field refusal above guards) -- it is the disk saying no to a
    # valid request, the same class `tests/test_web.py` pins at 500 on the
    # sibling prompt routes.
    assert r.status_code == 500
    assert "could not be removed" in r.text
    assert "could not be written" not in r.text
    assert "nothing was saved" in r.text.lower()


def test_settings_refusal_arms_on_any_non_empty_hidden_field_value(
    cp949_client, tmp_path
):
    """#555 gate rev-2 (Critic). The refusal keyed on
    `relation_aliases_rendered_unreadable == "1"` -- an exact string match on
    client-controlled input. Measured: a forged `"0"` or `"true"` value
    bypassed the guard entirely (303, file deleted), because only the
    template's literal `value="1"` armed it; every other non-empty value
    disarmed a destructive-path guard. The template is the only real submitter
    of this field (so this was not live), but a guard on a delete should not
    rely on matching one exact string forever -- the fix is a truthy check:
    ANY non-empty value means the same thing (this render's box was empty
    because the file was unreadable), and only an ABSENT field means the other
    thing (a genuine clear). This test drives a value the template never
    sends, to pin that the guard still arms on it rather than silently
    treating it as absent."""
    root = tmp_path / "kb"
    alias_path = root / RELATION_ALIASES_RELPATH
    before = alias_path.read_bytes()

    r = cp949_client.post(
        "/settings/relation-aliases",
        data={
            "relation_aliases_text": "",
            "relation_aliases_rendered_unreadable": "0",
        },
    )
    assert r.status_code == 400
    assert alias_path.exists()
    assert alias_path.read_bytes() == before


def test_settings_save_catches_a_non_oserror_write_failure(
    healthy_client, tmp_path, monkeypatch
):
    """#555 gate rev-2 (Critic N7), strengthened per gate rev-6 (Reviewer nit
    4). The write/delete guard is `except Exception`, not `except OSError` --
    matching the house form already used by `save_prompt_route`/
    `reset_prompt_route` for the same reason: this route has no narrower a
    claim to make about how its own filesystem calls can fail than those
    routes make about theirs.

    Injected at `os.replace` -- the LAST step in `_write_relation_aliases_atomic`,
    not the first. An earlier version of this test patched `tempfile.mkstemp`
    instead, where "nothing was saved" is trivially true because no temp file
    was ever created there. `os.replace` runs only after `mkstemp`, `fchmod`,
    `fdopen`, `write`, `flush` and `fsync` have all already succeeded, so a
    real temp file exists on disk at the moment this failure fires -- the
    interesting case, where the ORIGINAL file must still survive untouched and
    the now-orphaned temp file must be cleaned up rather than left behind, not
    just where nothing had happened yet to undo."""

    def boom(*args, **kwargs):
        raise ValueError("not an OSError")

    monkeypatch.setattr("verinote.web.app.os.replace", boom)

    root = tmp_path / "kb"
    alias_path = root / RELATION_ALIASES_RELPATH
    before = alias_path.read_bytes()

    r = healthy_client.post(
        "/settings/relation-aliases", data={"relation_aliases_text": "- 소속 -> role"}
    )
    # 500, not 400 (#555 gate rev-7): a server-side write failure, not a
    # client-request error -- see the guard's own comment for the argument.
    assert r.status_code == 500
    assert "could not be written" in r.text
    assert "nothing was saved" in r.text.lower()
    assert alias_path.read_bytes() == before
    leftovers = [p for p in alias_path.parent.iterdir() if p != alias_path]
    assert leftovers == []


def test_a_healthy_alias_file_still_fills_the_settings_textarea(healthy_client):
    r = healthy_client.get("/settings")
    assert r.status_code == 200
    assert PARSER_MSG not in r.text
    assert NAMED not in r.text
    assert "member_of" in r.text
    assert RENDERED_UNREADABLE_FIELD not in r.text


# ---------------------------------------------------------------------------
# R1 -- regression control: an absent alias file is not treated as a failure
# ---------------------------------------------------------------------------


def test_a_missing_alias_file_is_not_treated_as_a_failure(absent_client):
    """`store_relation_aliases` returns the parsed packaged defaults, and raises
    nothing, when the file does not exist (corroboration.py `not path.is_file()`
    branch). A guard that mistook "absent" for "broken" would put a banner on
    every healthy KB that never wrote this optional file."""
    for route in ("/", "/sources", "/settings"):
        r = absent_client.get(route)
        assert r.status_code == 200
        assert 'class="error"' not in r.text
        assert 'class="warn"' not in r.text
