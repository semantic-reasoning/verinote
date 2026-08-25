# SPDX-License-Identifier: MPL-2.0
r"""A broken `policy/relation-aliases.md` degrades `/`, `/sources`, `/settings`
instead of 500ing them (#555).

WHY THE MALFORMED TESTS ASSERT THE MESSAGE, NOT THE STATUS. Deleting the narrow
`except CorroborationPolicyError` clause (G1) inside `_trust_policy_failure`
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

SCOPE. Two issues' worth of routes live here, and the seam matters when reading
a failure. #555 covers `/`, `/sources` and `/settings`. #570 adds the fact-row
surface, in the Unit D section at the bottom: `GET /facts/{id}/row`,
`GET /facts/{id}/provenance`, `POST /facts/{id}/{toggle,accept,reject,amend}`
and `POST /sources/{id}/accept-all`. That last one is the violation #555 itself
shipped: `/sources` has rendered its `Accept all` form at 200 under both broken
inputs since `ef4b404`, over a POST that 500s whenever
`auto_accept_recommendations` is on — a default of False is the only reason a
single-configuration sweep read it as clean.

`/review` and `/workbench` are covered too, in the Unit E section. `/review`
degrades TWO different ways and the seam is the filter: on the default filter
the queue is real (`store.review_queue_page` reads no policy file) and only each
row's trust signals are withheld, while every other filter — `unsupported`,
`single-source`, `corroborated`, `conflicted` — selects facts BY the value that
could not be computed — so the row set, the total and
the pager are all alias-derived and the route refuses the question instead of
answering it emptily.

That refusal is why
`test_dashboard_offers_no_open_button_into_a_not_computed_queue_row` still pins
the suppressed Open buttons, on a justification that has changed: those targets
no longer crash. `/review?filter=unsupported` and `/review?filter=corroborated`
are two of the filters the route refuses, and `/workbench` withholds
both its tables — so a button from a "not computed" dashboard row into any of
them lands on a page that cannot answer the question the row poses. The button
is still not offered, now for a measured reason rather than an inherited one.

`store_typed_relations` (`policy/typed-relations.md`) was untouched by both
issues and is now covered by #585, in
`tests/test_typed_relations_web_guard.py`. The guard is SHARED rather than
duplicated: `_relation_alias_failure` became `_trust_policy_failure`, which
checks the alias file first and the typed file second and returns the first
failure, and every `alias_error` in this file's routes and templates became
`policy_error`. That rename is why the assertions here read "policy-file
notice" rather than "alias-file notice" and why no banner ends "Fix it on
Settings" any more — `settings.html` has an editor for the alias file and none
for the typed one.

Do not read the two files as parametrizations of each other. Their affected
route sets differ in BOTH directions, their malformed INPUT classes differ (a
line `typed_relations` cannot parse is silently skipped, not raised), and an
absent typed file degrades to `{}` — the same value a healthy KB with no typed
declarations produces — so #585's guard cannot pin itself on the rendered value
the way this file's can. The sibling file's docstring carries all three
measurements.

(#571, which an earlier draft of this paragraph cited for the typed-relations
hole, is a different defect in a different file: an unusable PATH at
`policy/relation-aliases.md` — a directory, a symlink loop — read as an absent
one.)
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
    assert r.text.count("Not computed — see the policy-file notice above.") == 2
    # The four alias-dependent queue rows show the not-computed marker, not a
    # digit -- plus one more occurrence inside the banner's own sentence, which
    # names the marker it is pointing at (MUST-FIX-3, #555 fix-round gate).
    assert r.text.count('<span class="badge muted">not computed</span>') == 5


def test_a_healthy_alias_file_still_shows_the_dashboard_corroboration_table(
    healthy_client,
):
    r = healthy_client.get("/")
    assert r.status_code == 200
    assert "Not computed — see the policy-file notice above." not in r.text
    assert '<span class="badge muted">not computed</span>' not in r.text
    # The healthy KB's one confirmed, source-backed fact appears in the table.
    assert "<code>A</code>" in r.text


def test_dashboard_offers_no_open_button_into_a_not_computed_queue_row(
    malformed_client,
):
    """MUST-FIX-2 (#555 fix-round gate). Before #555, a degraded row still
    carried a live `Open` button into `/review?filter=…` or `/workbench`.

    THE REASON HAS CHANGED AND THE TEST HAS NOT. When this was written those two
    targets 500ed, and a live button into a crash is the "looks healthy, isn't"
    shape criterion 3 exists to prevent. #570 guarded them, so they are now 200
    — and the suppression still stands, for a reason that is now measured rather
    than inherited: `unsupported` and `corroborated` are both filters `/review`
    REFUSES under a broken alias file, because each selects facts by a
    trust label that was not computed, and `/workbench` withholds both its
    tables. A button from a "not computed" row into any of the three would land
    on a page that cannot answer the question that row poses.

    The two alias-INDEPENDENT rows keep their buttons, and one of them is the
    check that this is not a blanket suppression: `/review` (unfiltered) is
    offered here and, since #570, answers at 200 with its real queue.
    """
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


# ---------------------------------------------------------------------------
# Unit D (#570) -- the fact-row surface: GET /facts/{id}/row,
# GET /facts/{id}/provenance, POST /facts/{id}/{toggle,accept,reject,amend}
# and POST /sources/{id}/accept-all.
# ---------------------------------------------------------------------------

# The row's "not computed" marker, and the verdict it must NOT borrow.
# `trust unavailable` is a MEASURED claim about the fact (it has no trust
# summary); the marker below means nobody measured. Keeping them distinct is
# what stops a broken alias file from being reported as a property of the fact.
ROW_NOT_COMPUTED = '<span class="badge muted">trust not computed</span>'
ROW_TRUST_UNAVAILABLE = "trust unavailable"
EVIDENCE_NOT_COMPUTED = '<span class="muted">not computed</span>'
NO_EVIDENCE_ANCHOR = "No evidence anchor"
# One of this fixture's `recommendation.reasons` caution chips -- the
# `{% for reason in recommendation.reasons[:2] %}` loop in `fact_row.html`,
# named by symbol rather than by line because this same diff moves that file.
# It is the only string on a fact row that moves when the accept
# RECOMMENDATION is withheld but the trust summary is not.
RECOMMENDATION_REASON = "insufficient distinct source support"
DOSSIER_NOT_COMPUTED = "Not computed — see the policy-file notice above."
# The lifecycle timeline's own wording. Deliberately not the shared marker:
# it is the only withheld section with no distinctive sentence, so under the
# shared one it could be pinned only by counting occurrences.
TIMELINE_NOT_COMPUTED = (
    "Extraction and review events not computed — see the policy-file notice above."
)

# (alias bytes, message that must be present, message that must be absent).
# The "absent" half is #555's G1 pin, not decoration: deleting the narrow
# `except CorroborationPolicyError` leaves every status at 200 and only swaps
# the bare parser message for the "could not be read" wrapper.
BROKEN_INPUTS = [
    pytest.param(MALFORMED_BYTES, PARSER_MSG, NAMED, id="malformed"),
    pytest.param(CP949_BYTES, NAMED, PARSER_MSG, id="cp949"),
]

AMEND_FORM = {
    # `*_kind="string"` deliberately: `_fact_input` accepts only "string" and
    # "term" and rejects anything else at `app.py` before a line of
    # alias-dependent code runs, so a form sending `kind="entity"` measures a
    # 400 and proves nothing about this guard. A7 asserts its own healthy
    # baseline is 200 for the same reason.
    "subject": "C",
    "relation": "소속",
    "object": "D2",
    "subject_kind": "string",
    "relation_kind": "string",
    "object_kind": "string",
    "note": "",
}


def _done_job(store, source_id: int) -> int:
    """A completed extraction job, so its source's facts clear the
    `source_analysis_incomplete` bar in `accept_recommendations`."""
    job_id = store.create_extraction_job(
        source_id=source_id, provider="fake", model="m", total_chunks=1
    )
    chunk_id = store.add_source_chunks(
        job_id=job_id, source_id=source_id, chunks=["body"]
    )[0]
    store.mark_extraction_job_running(job_id)
    store.mark_chunk_running(chunk_id)
    store.mark_chunk_done(chunk_id)
    store.finish_extraction_job(job_id)
    return job_id


def _auto_accept_client(tmp_path: Path, alias_bytes: bytes | None):
    """A KB on which the auto-accept rule really promotes a fact, and promotes it
    BECAUSE of the user's own alias file.

    `_open_app`'s KB cannot be used here, and not for want of a config flag:
    `apply_auto_accept_recommendations` promotes only review-tier rows
    (`accept_recommendations` iterates `store.facts(statuses=review_statuses())`),
    and that KB's only non-review fact is `confirmed` — engine tier, which the
    rule never touches. Its one review-tier fact is the one the request decides,
    which the rule excludes. So "the other fact was not promoted" holds there on
    a healthy file too, and asserts nothing.

    Here fact 2 (`A 소속 B`, needs_review, `a.txt`) is corroborated by fact 1
    (`A member_of B`, confirmed, `b.txt`) ONLY because `소속 -> member_of` makes
    them one canonical triple. Measured: healthy -> fact 2 becomes `accepted`;
    with no alias file at all -> it stays `needs_review`. Both sources carry a
    completed job or the recommendation is blocked by
    `source_analysis_incomplete` before corroboration is even weighed.
    """
    root = _make_kb(tmp_path)
    cfg = Config(
        root=root,
        db_path=root / "kb.sqlite",
        provider="anthropic",
        model="m",
        api_key=None,
        base_url=None,
        auto_accept_recommendations=True,
    )
    app = create_app(cfg)
    store = app.state.store
    source_a = store.add_source("sources/a.txt")
    job_a = _done_job(store, source_a)
    source_b = store.add_source("sources/b.txt")
    job_b = _done_job(store, source_b)
    store.add_fact(
        "A", "member_of", "B", status="confirmed", confidence=0.9,
        source_id=source_b, job_id=job_b,
    )
    store.add_fact(
        "A", "소속", "B", status="needs_review", confidence=0.5,
        source_id=source_a, job_id=job_a,
    )
    store.add_fact(
        "C", "소속", "D", status="needs_review", confidence=0.5,
        source_id=source_a, job_id=job_a,
    )
    if alias_bytes is not None:
        path = root / RELATION_ALIASES_RELPATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(alias_bytes)
    return TestClient(app, raise_server_exceptions=False), app


# --- GET /facts/{id}/row ---------------------------------------------------


def test_a_degraded_fact_row_still_renders_the_fact_and_its_actions(malformed_client):
    """Withholding trust must not withhold the fact (#570 AC-2).

    This is the route guard's own test: `_fact_row_context`'s deletion 500s the
    request, which has no body at all, while either TEMPLATE branch's deletion
    leaves this green — the row is still 200 and still carries every string
    below. toggle/accept/reject/edit are pure store writes and work fine under a
    broken alias file, so removing them (the shape #555 used for the dashboard's
    Open button, where the TARGET was down) would be the wrong fix here.
    """
    r = malformed_client.get("/facts/2/row")
    assert r.status_code == 200
    assert 'id="fact-2"' in r.text
    assert '<span class="subj term-string">&#34;C&#34;' in r.text
    assert '<span class="obj term-string">&#34;D&#34;' in r.text
    assert '<span class="badge badge-needs_review verdict">needs_review</span>' in r.text
    for control in (
        'hx-post="/facts/2/toggle"',
        'hx-post="/facts/2/accept"',
        'hx-post="/facts/2/reject"',
        'hx-get="/facts/2/edit"',
        'href="/facts/2/provenance"',
    ):
        assert control in r.text


def test_fact_row_survives_a_malformed_alias_file_and_keeps_the_parser_message(
    malformed_client,
):
    r = malformed_client.get("/facts/2/row")
    assert r.status_code == 200
    assert PARSER_MSG in r.text
    assert NAMED not in r.text


def test_fact_row_survives_a_cp949_alias_file_and_names_the_file(cp949_client):
    r = cp949_client.get("/facts/2/row")
    assert r.status_code == 200
    assert NAMED in r.text


def test_the_signals_cell_withholds_trust_rather_than_calling_it_unavailable(
    malformed_client,
):
    """The signals-cell branch's own test. Reverting it to `{% if trust %}` sends
    `trust=None` to the existing `{% else %}`, which prints the
    `trust unavailable` verdict — a healthy KB's sentence for a fact that has no
    trust summary — over a fact whose summary was never computed. The evidence
    cell's branch is not involved either way.

    ASSERTED ON `GET /facts/{id}/row`, AND THAT IS LOAD-BEARING. `review.html`
    includes this same partial, so the identical branch also sits behind
    `/review`'s own route guard. Asserting it there would give this test a
    SECOND outer guard whose deletion reddens it, and the signals cell would
    stop being independently falsifiable. Do not move it to `/review` for
    convenience.
    """
    r = malformed_client.get("/facts/2/row")
    assert ROW_NOT_COMPUTED in r.text
    assert ROW_TRUST_UNAVAILABLE not in r.text


def test_the_evidence_cell_claims_no_anchor_it_did_not_look_for(malformed_client):
    """The evidence-cell branch's own test, and it is a different cell from the
    one above: the anchor is read off `trust.evidence`, so with no summary the
    existing `{% else %}` asserts `No evidence anchor` about a fact nobody
    checked.

    On `GET /facts/{id}/row` for the same reason as the test above: this partial
    is also included by `review.html`, and asserting there would put a second
    outer guard behind this one.
    """
    r = malformed_client.get("/facts/2/row")
    assert NO_EVIDENCE_ANCHOR not in r.text
    assert EVIDENCE_NOT_COMPUTED in r.text


def test_a_healthy_alias_file_still_shows_the_fact_row_trust_badges(healthy_client):
    """Anti-vacuity control for the degraded fact-row tests above: the identity
    test, both message tests, and the signals- and evidence-cell tests.

    The last assertion is what makes the evidence-cell test non-vacuous:
    `No evidence anchor` is present on a HEALTHY row for this fixture's
    anchor-less fact (measured), so its absence under a broken file is a real
    change and not a string that was never there.
    """
    r = healthy_client.get("/facts/2/row")
    assert r.status_code == 200
    assert ROW_NOT_COMPUTED not in r.text
    assert EVIDENCE_NOT_COMPUTED not in r.text
    assert PARSER_MSG not in r.text
    assert NAMED not in r.text
    assert '<span class="badge chip">unsupported</span>' in r.text
    assert NO_EVIDENCE_ANCHOR in r.text


def test_a_healthy_fact_row_still_carries_its_accept_recommendation_reasons(
    healthy_client,
):
    """The fact-row guard's RECOMMENDATION half, on the axis its deletion test
    cannot reach.

    That guard does two things when the alias file is broken: it withholds
    `trust` AND it withholds `recommendation`. Over-applying the second —
    setting `recommendation = None` unconditionally, so recommendations are
    never computed at all — is invisible to every other test in this file. The
    healthy control above pins the `unsupported` chip, which comes off
    `trust.trust_labels`, not off the recommendation; the degraded tests pin
    strings that are absent either way.

    NOT a duplicate of the equivalent `/review` control. The two pages take
    their recommendations from DIFFERENT calls — `/review` from
    `accept_recommendations_for`, this row from the `accept_recommendations`
    fallback inside `_fact_row_context` — so each mutation is invisible to the
    other page's test. Measured on this healthy fixture:

        build                              /review  /facts/2/row
        correct                            present  present
        accept_recommendations_for -> {}   ABSENT   present
        accept_recommendations     -> {}   present  ABSENT
        fact_trust_summary         -> None ABSENT   ABSENT

    Scope of what this string proves, because it is narrower than the test name
    suggests: the caution chips are rendered inside the `{% elif trust %}` arm
    of `fact_row.html`, so its presence means trust AND recommendations were
    computed. That holds on a healthy fixture and is why one assertion can carry
    both. A refactor that moves those chips out of that arm drops the trust half
    of the implication without reddening anything — at which point the trust
    half needs a pin of its own.
    """
    r = healthy_client.get("/facts/2/row")
    assert r.status_code == 200
    assert RECOMMENDATION_REASON in r.text


# --- the decision POSTs ----------------------------------------------------


@pytest.mark.parametrize("alias_bytes, present, absent", BROKEN_INPUTS)
def test_accept_survives_a_broken_alias_file(tmp_path, alias_bytes, present, absent):
    client = _client(tmp_path, alias_bytes)
    r = client.post("/facts/2/accept")
    assert r.status_code == 200
    assert present in r.text
    assert absent not in r.text


@pytest.mark.parametrize("route", ["toggle", "reject"])
@pytest.mark.parametrize("alias_bytes, present, absent", BROKEN_INPUTS)
def test_toggle_and_reject_survive_a_broken_alias_file(
    tmp_path, route, alias_bytes, present, absent
):
    client = _client(tmp_path, alias_bytes)
    r = client.post(f"/facts/2/{route}")
    assert r.status_code == 200
    assert present in r.text
    assert absent not in r.text


@pytest.mark.parametrize("alias_bytes, present, absent", BROKEN_INPUTS)
def test_amend_survives_a_broken_alias_file(
    tmp_path, healthy_client, alias_bytes, present, absent
):
    """`POST /facts/{id}/amend` — the endpoint #570's original table omits. It
    reaches the same `_fact_row_context` -> `fact_trust_summary` stack as
    accept/reject/toggle, through `_row_after_decision`.

    The healthy baseline is asserted here, not assumed: this form 400s on a
    rejected kind before any alias-dependent code runs, and a 400 baseline would
    make the broken-input assertion below measure an early return rather than
    this guard.
    """
    baseline = healthy_client.post("/facts/2/amend", data=AMEND_FORM)
    assert baseline.status_code == 200

    # `healthy_client` already built a KB under `tmp_path`; the broken one needs
    # its own root.
    broken_root = tmp_path / "broken"
    broken_root.mkdir()
    client = _client(broken_root, alias_bytes)
    r = client.post("/facts/2/amend", data=AMEND_FORM)
    assert r.status_code == 200
    assert present in r.text
    assert absent not in r.text


def _superseded_amend(tmp_path: Path, monkeypatch, alias_bytes: bytes):
    """Drive `amend_fact`'s `except TerminalFactError` exit. See the two tests
    below for what this patch neutralises and what it therefore costs."""
    client = _client(tmp_path, alias_bytes)
    assert client.post("/facts/2/reject").status_code == 200
    monkeypatch.setattr(
        "verinote.web.app.is_actionable_fact_status", lambda _status: True
    )
    return client.post("/facts/2/amend", data=AMEND_FORM)


@pytest.mark.parametrize("alias_bytes, present, absent", BROKEN_INPUTS)
def test_a_superseded_amend_also_survives_a_broken_alias_file(
    tmp_path, monkeypatch, alias_bytes, present, absent
):
    """`amend_fact` has TWO row-rendering exits and this one is not the success
    path: `except TerminalFactError` re-renders the read-only row directly
    through `_row`, bypassing `_row_after_decision` entirely. Guarding only the
    success path leaves this exit 500ing while the test above passes.

    HOW THIS REACHES THAT EXIT, AND WHAT THE PATCH COSTS. In production the exit
    is reachable only through a TOCTOU window: a reject landing between
    `amend_fact`'s `_actionable_fact_or_error` pre-check and its
    `store.amend_fact` call. It is NOT reachable from a stale edit form — the
    pre-check reads the fact's CURRENT status on this request, so a form left
    open while someone else rejects the fact just gets a plain 400 (measured:
    reject-then-amend is 400 on every input, healthy included).

    Simulating that window by monkeypatching the module-level
    `is_actionable_fact_status` neutralises MORE than the pre-check.
    `verinote.web.app` binds that name once and TWO callers read it: the
    pre-check, and `_fact_row_context`'s own `actionable` computation. So the
    row rendered below carries a `superseded` badge together with live
    accept/reject/toggle buttons and no `rejected — no further action` text — a
    combination the real race cannot produce, because there `_fact_row_context`
    re-reads the row, sees `superseded`, and renders the read-only form. Both
    sides of that were measured.

    Which is why every assertion here is `actionable`-INDEPENDENT: the status,
    the alias message, the row id, the withheld-trust marker. Do not add an
    assertion about the action buttons or the no-further-action text; it would
    pin a state production cannot reach. Only the `TerminalFactError` is
    genuine — raised by the real store on a real superseded row, with the
    handler and the row render running unmodified.
    """
    r = _superseded_amend(tmp_path, monkeypatch, alias_bytes)
    assert r.status_code == 200
    assert present in r.text
    assert absent not in r.text
    assert ROW_NOT_COMPUTED in r.text


@pytest.mark.parametrize("alias_bytes, present, absent", BROKEN_INPUTS)
def test_a_superseded_amend_still_renders_a_row_at_all(
    tmp_path, monkeypatch, alias_bytes, present, absent
):
    """The SEPARATING test for `amend_fact`'s second-exit threading, and the
    reason it is split off from the message test above.

    Status and row identity are the only observations of this exit that survive
    a template guard's deletion. Delete the signals-cell branch and this request
    is still 200 with `id="fact-2"`, losing only the marker and the parser
    message — so the message test above is reddened by a TEMPLATE guard and
    cannot be this route-level guard's own evidence. This one can.

    The reddening population, ENUMERATED over every deletable unit in this
    change rather than quantified over an unnamed set. Exactly three deletions
    redden this test. Two 500 the amend response itself: this exit's own
    `policy_error` read, and `_fact_row_context`'s withholding branch, which
    every fact-row render passes through. The third is `_row_after_decision`'s
    read, and it fails EARLIER than the exit under test — `_superseded_amend`'s
    `assert client.post(...reject...).status_code == 200` precondition 500s
    before the amend is ever sent. Every template guard in this change leaves it
    green — the fact-row signals and evidence cells, and on `provenance.html`
    the alias banner and the Trust-summary, Evidence-summary, Conflict, Timeline
    and Source-evidence branches. Named, not counted: splitting one more dossier
    section, which is exactly what this change did to the timeline, would make a
    bare number here wrong without reddening anything. That green-under-all
    property is the one the split exists for.

    Keep the two assertions below to status and identity. Adding the message or
    the marker here would merge this test back into the one above and undo the
    split.
    """
    del present, absent
    r = _superseded_amend(tmp_path, monkeypatch, alias_bytes)
    assert r.status_code == 200
    assert 'id="fact-2"' in r.text


# --- GET /facts/{id}/provenance -------------------------------------------


def test_provenance_survives_a_malformed_alias_file_and_keeps_the_parser_message(
    malformed_client,
):
    r = malformed_client.get("/facts/2/provenance")
    assert r.status_code == 200
    assert PARSER_MSG in r.text
    assert NAMED not in r.text


def test_provenance_survives_a_cp949_alias_file_and_names_the_file(cp949_client):
    r = cp949_client.get("/facts/2/provenance")
    assert r.status_code == 200
    assert NAMED in r.text


def test_provenance_withholds_the_dossier_but_keeps_the_fact_identity(
    malformed_client,
):
    """`provenance` calls `fact_trust_summary` DIRECTLY, so no `policy_error`
    threaded through `_fact_row_context` reaches it (#570 trap 1) — and its
    route guard and template branches are ONE guard, because
    `{{ trust.support.source_count }}` is a two-deep attribute of `None` and
    Jinja raises `UndefinedError` on that: withholding `trust` without the
    template branches swaps one 500 for another.

    The three sentences asserted absent are not `trust.` references — they are
    `{% else %}` prose that renders happily on `trust=None` and states, in
    order, that a conflict search found nothing, that no evidence anchors are
    recorded, and that the fact was seeded or hand-entered. None of the three
    was measured on this KB.
    """
    r = malformed_client.get("/facts/2/provenance")
    assert "canonical relation" not in r.text
    assert '<span class="badge chip">unsupported</span>' not in r.text
    # One marker per withheld section that shares the standard wording: trust
    # summary, evidence summary, conflict summary, source evidence. The
    # timeline's withheld middle has its own sentence and its own test.
    assert r.text.count(DOSSIER_NOT_COMPUTED) == 4
    # The identity is not withheld with the dossier.
    assert "Trust dossier — fact #2" in r.text
    assert '<span class="subj term-string">&#34;C&#34;' in r.text
    assert '<span class="obj term-string">&#34;D&#34;' in r.text
    assert '<span class="badge badge-needs_review">needs_review</span>' in r.text


# The sections below are asserted one test each, not folded into the test
# above. Each is a separately deletable `{% if %}` in `provenance.html`, and
# with all of them asserted in one test every one of those deletions reddens
# the same single test — which makes them indistinguishable in the
# falsifiability matrix even though each is a place the code can get this wrong
# on its own. No count here on purpose: `provenance.html` carries guards beyond
# these sections (the alias banner, the Trust-summary wrapper), so a bare number
# would be both wrong now and staler after the next split.


def test_the_dossier_does_not_deny_a_conflict_it_never_searched_for(
    malformed_client,
):
    """`{% if trust.conflict %}`'s `{% else %}` renders happily on `trust=None`
    (Jinja: `None.conflict` is falsy Undefined, it does not raise) and states
    that this fact has no single-valued conflict — over a search that never
    ran."""
    r = malformed_client.get("/facts/2/provenance")
    assert "No deterministic single-valued conflict for this fact." not in r.text


def test_the_dossier_does_not_deny_evidence_anchors_it_never_looked_for(
    malformed_client,
):
    """Same shape, Source-evidence section: the `{% else %}` claims no anchors
    are recorded for a fact whose anchors were never read."""
    r = malformed_client.get("/facts/2/provenance")
    assert "No source evidence anchors recorded for this fact." not in r.text


def test_the_dossier_does_not_call_an_uncomputed_origin_hand_entered(
    malformed_client,
):
    """Evidence-summary section. Its run row falls through to
    `— (seeded or hand-entered)`, which is a positive claim about where the
    fact came from, asserted about an origin nobody looked up."""
    r = malformed_client.get("/facts/2/provenance")
    assert "(seeded or hand-entered)" not in r.text


def test_the_degraded_timeline_says_its_middle_is_missing(malformed_client):
    """The Lifecycle timeline keeps `created` and `updated` (both off `f`) and
    loses everything between them, which is read out of the trust summary. With
    no row of its own it would close silently and read as a fact nothing ever
    happened to.

    This section is why the marker here has its own wording: it is the only
    withheld section with no distinctive sentence, so under the shared marker it
    was pinned by an occurrence COUNT alone — and a count reddens for whichever
    section went missing, naming none of them.
    """
    r = malformed_client.get("/facts/2/provenance")
    assert TIMELINE_NOT_COMPUTED in r.text


def test_a_healthy_alias_file_still_shows_the_trust_dossier(healthy_client):
    """Anti-vacuity control for the degraded-dossier tests: each string they
    assert ABSENT under a broken alias file is asserted present here, so every
    one of those absences is a real change rather than a string this page never
    had. Paired by name, since a positional reference does not survive a split:

      `canonical relation`                 <- ..._withholds_the_dossier_but_keeps_the_fact_identity
      `No deterministic single-valued ...` <- ..._does_not_deny_a_conflict_it_never_searched_for
      `No source evidence anchors ...`     <- ..._does_not_deny_evidence_anchors_it_never_looked_for
      `(seeded or hand-entered)`           <- ..._does_not_call_an_uncomputed_origin_hand_entered

    The two not-computed markers go the other way: they must not appear on a
    healthy page at all, and `..._degraded_timeline_says_its_middle_is_missing`
    asserts the timeline one present when the file is broken.

    (An earlier version of this docstring said "the test above". Splitting the
    degraded assertions into one test per section silently invalidated that,
    without reddening anything — hence the names.)
    """
    r = healthy_client.get("/facts/2/provenance")
    assert r.status_code == 200
    assert DOSSIER_NOT_COMPUTED not in r.text
    assert TIMELINE_NOT_COMPUTED not in r.text
    assert "canonical relation" in r.text
    assert "No deterministic single-valued conflict for this fact." in r.text
    assert "No source evidence anchors recorded for this fact." in r.text
    assert "(seeded or hand-entered)" in r.text


# --- the auto-accept write path -------------------------------------------


@pytest.mark.parametrize("alias_bytes, present, absent", BROKEN_INPUTS)
def test_accept_all_source_facts_survives_a_broken_alias_file_when_auto_accept_is_on(
    tmp_path, alias_bytes, present, absent
):
    """#555 shipped this one broken: `/sources` has rendered
    `action="/sources/1/accept-all"` at 200 under both broken inputs since
    `ef4b404`, over a POST that 500s whenever `auto_accept_recommendations` is
    on. `auto_accept_recommendations` defaults to False, which is why a
    single-configuration sweep reads this endpoint as clean.

    This route never enters `_fact_row_context`, so the fact-row guard's
    deletion leaves it at 303 — it is the auto-accept guard's own test.
    """
    del present, absent  # a 303 carries no body to assert a message on
    client, _app = _auto_accept_client(tmp_path, alias_bytes)
    r = client.post("/sources/1/accept-all", follow_redirects=False)
    assert r.status_code == 303


@pytest.mark.parametrize("alias_bytes, present, absent", BROKEN_INPUTS)
def test_a_broken_alias_file_stops_the_auto_accept_pass_rather_than_running_it_on_defaults(
    tmp_path, alias_bytes, present, absent
):
    """The strongest form of #570 AC-2, because this one is a WRITE.
    `apply_auto_accept_recommendations` promotes facts to `accepted` and retracts
    lapsed ones, and it decides which by reading the alias file. A badge computed
    on the wrong rules is re-rendered next request; a status transition is
    committed and audited.

    Fact 2 is promoted here only because the user's `소속 -> member_of` line
    makes it corroborated by two distinct sources — the healthy half below is
    what proves this fixture can see a promotion at all.

    Stated limit: "fact 2 is still needs_review" is also what a pass run under
    bare defaults produces (measured with no alias file present). That mode is
    not reachable at this site, because `store_relation_aliases` RAISES on both
    of these inputs rather than returning defaults, so there is no third build
    to tell apart. This test's falsifying build is the guard's deletion, which
    500s the request.
    """
    del present, absent

    (tmp_path / "healthy").mkdir()
    (tmp_path / "broken").mkdir()
    healthy, healthy_app = _auto_accept_client(tmp_path / "healthy", HEALTHY_BYTES)
    assert healthy.post("/facts/3/accept").status_code == 200
    assert healthy_app.state.store.get_fact(2)["status"] == "accepted"

    client, app = _auto_accept_client(tmp_path / "broken", alias_bytes)
    r = client.post("/facts/3/accept")
    assert r.status_code == 200
    assert app.state.store.get_fact(2)["status"] == "needs_review"


# ---------------------------------------------------------------------------
# Unit E (#570) -- the review surface: GET /review, on the default filter and
# on each trust-label filter, and GET /workbench.
# ---------------------------------------------------------------------------

REVIEW_FILTER_NAV = '<nav class="filters"'
WORKBENCH_H1 = "<h1>Trust workbench</h1>"
REVIEW_SHOWING_NONE = "Showing 0 of 0 review facts"
REVIEW_NO_MATCH = "No facts match this filter."
REVIEW_PAGINATION = '<nav class="pagination"'
REVIEW_FILTER_REFUSAL = (
    "This filter selects facts by trust label, which is not computed"
)
WORKBENCH_NO_CORROBORATION = (
    "No facts are corroborated by multiple distinct sources."
)
WORKBENCH_NO_CONFLICTS = "No source-backed single-valued conflicts."


def _empty_queue_client(tmp_path: Path, alias_bytes: bytes | None) -> TestClient:
    """A `/review` whose queue is deliberately empty -- see the test that uses it
    for why that is an instrument rather than an oversight."""
    root = _make_kb(tmp_path)
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
    if alias_bytes is not None:
        path = root / RELATION_ALIASES_RELPATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(alias_bytes)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def corroborated_client(tmp_path: Path) -> TestClient:
    """A KB whose `/workbench` renders a real corroboration table on a healthy
    alias file.

    `_open_app`'s KB cannot serve as the workbench anti-vacuity control: it has
    one source, so nothing is corroborated by two distinct sources and a healthy
    `/workbench` shows the same empty-state prose a withheld one would have to
    avoid (measured). Here `A/소속/B` from `a.txt` and `A/member_of/B` from
    `b.txt` are one canonical triple *because of the user's alias line*, which is
    what puts a `<table class="counts">` on the healthy page.
    """
    root = _make_kb(tmp_path)
    app = create_app(
        Config(
            root=root,
            db_path=root / "kb.sqlite",
            provider="anthropic",
            model="m",
            api_key=None,
            base_url=None,
        )
    )
    store = app.state.store
    source_a = store.add_source("sources/a.txt")
    source_b = store.add_source("sources/b.txt")
    store.add_fact(
        "A", "소속", "B", status="confirmed", confidence=0.9, source_id=source_a
    )
    store.add_fact(
        "A", "member_of", "B", status="confirmed", confidence=0.9, source_id=source_b
    )
    store.add_fact(
        "C", "소속", "D", status="needs_review", confidence=0.5, source_id=source_a
    )
    path = root / RELATION_ALIASES_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(HEALTHY_BYTES)
    return TestClient(app, raise_server_exceptions=False)


# --- GET /review, default filter -------------------------------------------


def test_review_survives_a_malformed_alias_file_and_keeps_the_parser_message(
    malformed_client,
):
    r = malformed_client.get("/review")
    assert r.status_code == 200
    assert PARSER_MSG in r.text
    assert NAMED not in r.text


def test_review_survives_a_cp949_alias_file_and_names_the_file(cp949_client):
    r = cp949_client.get("/review")
    assert r.status_code == 200
    assert NAMED in r.text


def test_review_stays_up_with_an_empty_queue_under_a_broken_alias_file(tmp_path):
    """The `/review` route guard's own test, and the ONLY fixture in which its
    failure is observable.

    STATUS-ONLY, PERMANENTLY. Do not add a message or banner assertion here.
    The banner is a separate guard, and asserting its text would make this test
    redden when the banner is deleted too — which is exactly the uniqueness this
    test exists to have. Assert the status and nothing else.

    THIS FIXTURE IS DELIBERATELY EMPTY -- one `confirmed` fact and no
    `needs_review` fact -- and adding a `needs_review` fact destroys what the
    test pins, silently, without reddening anything. AC-4 (every `/review`
    fixture must contain a `needs_review` fact) has been ruled by its own author
    NOT to govern this test: AC-4 exists to stop a *degradation* assertion
    passing vacuously on a queue that never reaches `_fact_row_context`, and this
    test asserts no degradation. It asserts that the route survives at all, and a
    populated queue provably cannot pin that. The carve-out is this one test
    wide; AC-4 still governs every other `/review` test in this file.

    The measurement, verbatim, because someone re-deriving this in six months
    needs the evidence rather than the conclusion:

        A-G1 deleted, B-G1 intact, queue populated=True   -> GET /review = 500
        A-G1 deleted, B-G1 intact, queue populated=False  -> GET /review = 200

    On a populated queue the fact-row guard's site (`fact_trust_summary`, via
    `_fact_row_context`) is reached and its deletion 500s the page, so every
    populated-queue `/review` test is reddened by that guard too. On an empty
    queue that site is never reached, while this route's own site
    (`accept_recommendations_for`, which builds its engine off the alias file
    before it looks at a single id) still is.
    """
    client = _empty_queue_client(tmp_path, MALFORMED_BYTES)
    assert client.get("/review").status_code == 200


def test_review_still_lists_the_queue_rows_and_withholds_only_their_trust(
    malformed_client,
):
    """`store.review_queue_page` reads no policy file, so the rows on the default
    filter are the KB's real ones — only the trust signals on them are withheld.
    A guard that dropped the queue as well would pass every status assertion and
    tell the user their review queue was empty."""
    r = malformed_client.get("/review")
    assert 'id="fact-2"' in r.text
    assert ROW_NOT_COMPUTED in r.text


def test_the_review_fixture_actually_reaches_a_fact_row(healthy_client):
    """AC-4's control, paired with the test above: a `/review` whose queue never
    reaches the row partial would pass the degradation tests against a half-fix.
    Asserted on the HEALTHY client so it measures the fixture, not the guard."""
    r = healthy_client.get("/review")
    assert r.status_code == 200
    assert 'id="fact-2"' in r.text
    assert "Showing 1-1 of 1" in r.text


# --- GET /review, the trust-label filters ----------------------------------


def test_a_trust_label_filter_stays_up_and_keeps_its_filter_nav(malformed_client):
    """The filter-refusal guard's own test, and the assertion is chosen so that
    no template deletion can redden it.

    The filter nav sits above the banner, the toolbar, the status line, both
    pagination calls and the queue block, and is built from static labels — so it
    renders on every degraded page and survives the deletion of every template
    guard on it — the banner, the pager block and the queue block. It is also green under the fact-row guard's
    deletion, because with `queue = None` no row context is ever built. That
    leaves the route's own refusal as the only deletion that reddens it.
    """
    r = malformed_client.get("/review?filter=unsupported")
    assert r.status_code == 200
    assert REVIEW_FILTER_NAV in r.text


def test_the_filtered_page_reports_no_total_it_did_not_compute(malformed_client):
    """The pager guard's own test. With `pager = None` and no guard, Jinja
    renders `{{ pager.start }}` as '' and iterates `pager.pages` as empty without
    raising, so the page comes back 200 saying it is showing 0 of 0 review facts
    — a count of a queue nobody built — with a pagination nav offering Previous
    and Next through it.

    Both strings are asserted because the guard has two halves (the
    toolbar-and-status block, and the two `review_pages(pager)` calls) and
    deleting either alone would otherwise be silent.
    """
    r = malformed_client.get("/review?filter=unsupported")
    assert REVIEW_SHOWING_NONE not in r.text
    assert REVIEW_PAGINATION not in r.text


def test_the_filtered_page_does_not_claim_the_filter_matched_nothing(
    malformed_client,
):
    """The queue guard's own test. `{% if queue %}` alone cannot tell `None` from
    an empty list, so it falls through to "No facts match this filter." — a claim
    about which facts carry this trust label, made about a label that was never
    computed.

    The refusal sentence lives in this block and only in this block, so it is
    asserted here and nowhere else; putting it in the pager block too would leave
    it on the page when either block alone is deleted.
    """
    r = malformed_client.get("/review?filter=unsupported")
    assert REVIEW_NO_MATCH not in r.text
    assert REVIEW_FILTER_REFUSAL in r.text


@pytest.mark.parametrize("alias_bytes, present, absent", BROKEN_INPUTS)
def test_the_filtered_page_names_the_alias_failure(
    tmp_path, alias_bytes, present, absent
):
    """The review banner's own test. On this page the banner is the sole carrier
    of the reason: `review.html`'s only `{% include "partials/fact_row.html" %}`
    sits inside `{% for row in queue %}`, and with `queue = None` that loop never
    runs, so the fragment's own reason line renders nowhere."""
    client = _client(tmp_path, alias_bytes)
    r = client.get("/review?filter=unsupported")
    assert r.status_code == 200
    assert present in r.text
    assert absent not in r.text


def test_a_healthy_alias_file_still_filters_the_review_queue_by_trust_label(
    healthy_client,
):
    """Anti-vacuity control for the filter tests above: a route that refused the
    filter unconditionally would pass all of them.

    `unsupported` is the only non-vacuous choice on this fixture. `single-source`,
    `corroborated` and `conflicted` each render `Showing 0 of 0` *and* "No facts
    match this filter." on a HEALTHY file (measured, each of the four named here),
    so swapping the
    filter here would make this control itself vacuous.
    """
    r = healthy_client.get("/review?filter=unsupported")
    assert r.status_code == 200
    assert 'id="fact-2"' in r.text
    assert "Showing 1-1 of 1" in r.text


def test_a_healthy_review_page_still_carries_its_accept_recommendations(
    healthy_client,
):
    """The review guard's RECOMMENDATION half, on the over-application axis its
    deletion test cannot reach.

    That guard does two things when the file is broken: it withholds the accept
    recommendations and it withholds each row's trust. Mutating it to "never
    compute recommendations at all" is invisible to every other test here — the
    healthy control above pins the queue and the total, both of which are
    alias-independent, and the degraded tests pin strings that are absent either
    way.

    Same conjunction caveat as the fact-row equivalent: the caution chips are
    rendered inside the trust arm of `fact_row.html`, so this asserts that trust
    AND recommendations were computed, not recommendations alone.
    """
    r = healthy_client.get("/review")
    assert r.status_code == 200
    assert RECOMMENDATION_REASON in r.text


# --- GET /workbench ---------------------------------------------------------


@pytest.mark.parametrize("alias_bytes, present, absent", BROKEN_INPUTS)
def test_workbench_stays_up_under_a_broken_alias_file(
    tmp_path, alias_bytes, present, absent
):
    """The workbench route guard's own test, and like the filter nav the
    assertion is chosen to be above every template guard: the `<h1>` precedes
    the banner, the corroborated-table branch and the conflicts-table branch, so
    no template deletion can redden it.

    Parametrized over both inputs deliberately. This is the only place either
    input's *status* on `/workbench` is asserted — the banner test below asserts
    the message, and a message assertion only implies a 200 rather than pinning
    it.
    """
    del present, absent
    client = _client(tmp_path, alias_bytes)
    r = client.get("/workbench")
    assert r.status_code == 200
    assert WORKBENCH_H1 in r.text


@pytest.mark.parametrize("alias_bytes, present, absent", BROKEN_INPUTS)
def test_workbench_names_the_alias_failure(tmp_path, alias_bytes, present, absent):
    """The workbench banner's own test. `workbench.html` contains no
    `{% include %}` at all, so unlike the review queue there is no fragment that
    could carry the reason instead — the banner is its only possible source."""
    client = _client(tmp_path, alias_bytes)
    r = client.get("/workbench")
    assert present in r.text
    assert absent not in r.text


# The two tables below get one test each, not one test asserting both. They are
# separately deletable `{% if workbench is none %}` branches, and folded into a
# single test each deletion reddens the same one — which leaves the two
# indistinguishable in the falsifiability matrix even though either can be got
# wrong on its own. The occurrence count keeps its own test for the same reason:
# it reddens under both, so holding it alongside either table's assertion would
# take that table's uniqueness away again.


def test_the_workbench_does_not_deny_corroboration_it_never_computed(
    malformed_client,
):
    """`workbench.corroborated` on a `None` workbench is falsy Undefined rather
    than a raise, so without its `is none` branch the page renders 200 asserting
    that no fact is corroborated by multiple distinct sources — over a KB whose
    corroboration was never computed. One of the exact falsehoods #555 removed
    from the dashboard."""
    r = malformed_client.get("/workbench")
    assert WORKBENCH_NO_CORROBORATION not in r.text


def test_the_workbench_does_not_deny_conflicts_it_never_searched_for(
    malformed_client,
):
    """The same shape one table down, and a separate deletion: the conflicts
    section's `{% else %}` claims there are no source-backed single-valued
    conflicts, about a search that never ran."""
    r = malformed_client.get("/workbench")
    assert WORKBENCH_NO_CONFLICTS not in r.text


def test_both_withheld_workbench_tables_say_they_were_not_computed(
    malformed_client,
):
    """The positive half of the corroboration and conflicts tests above: each
    withheld table must say so
    rather than silently rendering nothing, which would read as a page with no
    findings."""
    r = malformed_client.get("/workbench")
    assert r.text.count(DOSSIER_NOT_COMPUTED) == 2


def test_a_healthy_alias_file_still_shows_the_workbench_tables(corroborated_client):
    """Anti-vacuity control for the test above: a route that withheld the tables
    unconditionally would also pass it. Uses the two-source fixture, because on a
    single-source KB the healthy page shows the same empty-state prose the
    degraded page must avoid."""
    r = corroborated_client.get("/workbench")
    assert r.status_code == 200
    assert '<table class="counts">' in r.text
    assert WORKBENCH_NO_CORROBORATION not in r.text
    assert DOSSIER_NOT_COMPUTED not in r.text


# --- AC-3: the control on a guarded page must not lead to a crash -----------


def test_the_dashboard_open_button_into_the_review_queue_reaches_a_working_page(
    malformed_client,
):
    """AC-3, asserted as a path rather than as two independent facts.

    #555 guarded `/` and left this Open button live over a `/review` that 500ed:
    the dashboard rendered honestly at 200 and its own control crashed. Every
    other test here requests `/review` directly, which cannot see that — a route
    can be non-500 on a direct request while the page that links to it never
    offers the link, and it could equally be offered while broken. This walks the
    one step: read the button off the dashboard, then follow it.
    """
    dashboard = malformed_client.get("/")
    assert dashboard.status_code == 200
    assert '<a class="btn ghost" href="/review">Open</a>' in dashboard.text
    assert malformed_client.get("/review").status_code == 200
