# SPDX-License-Identifier: MPL-2.0
r"""A broken `policy/typed-relations.md` degrades the web surface instead of
500ing it (#585).

This is the sibling of `test_relation_aliases_web_guard.py`, and the two files
are deliberately NOT parametrizations of each other. Read the four paragraphs
below before adding to either: three of them are measurements that make an
"obvious" simplification of this file silently stop proving anything.

WHY THE MALFORMED INPUT IS A DUPLICATE ALIAS, AND WHY THE REASON HAS CHANGED.
When this file was written `typed_relations` was the opposite of its sibling: a
line that did not match `_TYPED_REL_RE`, or whose type tag was outside
`_TYPED_TYPES = {date, number, ordinal, amount}`, hit `continue` and the
declaration was dropped with no error and no 500. So a test that planted the
alias file's malformed bytes here saw 200, passed, and pinned NOTHING -- the
file had parsed cleanly to `{}`. `TYPED_DUP_ALIAS` below is a duplicate alias
for that reason.

#589 closed that path. A line that is neither blank nor a `#` comment must now
parse, which is `relation_aliases`' rule adopted rather than re-invented.
SEVEN conditions raise, and this list is not maintained by hand --
`test_the_raise_conditions_are_derived_from_the_source_not_listed_by_hand`
derives them from `corroboration.py` by AST and fails if this paragraph stops
naming one of them:

    unparseable line, unknown type tag, duplicate alias, units on a
    non-`amount` type, malformed unit pair, non-numeric unit value,
    invalid unit mapping

Blanks, `#` comments and `##` markdown headings are still tolerated -- headings
survive because they begin with `#`.

THE COUNT HAS BEEN WRONG TWICE AND BOTH TIMES IT WAS WRONG WHEN WRITTEN, not
stale. #585 said "exactly four SEMANTIC conditions"; five raised on the tree it
described. #589 replaced it with six; seven raised. The omission was the same
one both times -- `invalid unit mapping`, which `_parse_amount_units` raises on
an empty unit name, a non-integral value, or a value <= 0. A hand-written list
rots exactly the way a count does, which is why one is derived now instead.

So the hazard that shaped this file is gone: no non-comment input degrades
silently to `{}` any more, and the malformed slot cannot be quietly filled with
something vacuous. What still needs pinning is narrower, and
`test_the_parser_reports_a_typo_instead_of_dropping_it` carries it: the
duplicate-alias message differs from the unparseable-line message, so asserting
the MESSAGE keeps `TYPED_DUP_ALIAS` tied to the condition it is named for
rather than to "something raised".

WHY THE FIXTURE IS AN `amount` DECLARATION.
The TYPE has to be `amount` because it is the only one of the four that makes
this fixture's two objects one value: `normalize_typed_value` maps both `1억`
and `100000000원` to `100000000`, while `date`, `number` and `ordinal` each
return `None` for BOTH of them, leaving two unrelated raw keys. Every
corroboration control below rests on that merge.

THE DECLARED NAME NO LONGER MATTERS, AND RETIRING THAT RULE IS #589's POINT.
Until #589 the name also had to be one the alias table leaves alone.
`typed_relations()` keys its dict on the RAW name written in the file, while
every consumer canonicalizes before looking it up, so a declaration whose name
appears in the alias table was stored under a key it was never read back by and
was dropped without a word. This paragraph carried a four-row table of `None`s
showing exactly that. #589 made every consumer resolve through the alias table,
so the two columns now agree -- re-derived here, declaration and fact sharing
the name in each column:

    type     object         declared on 설립일   declared on 자본금
    date     '2020.03.01'   20200301             20200301
    amount   '1억'          100000000            100000000
    number   '3'            3000                 3000
    ordinal  '3위'          3                    3

Declaring `date` on the canonical `established_on` while the fact's relation is
still `설립일` also yields 20200301, as it did before -- that path was never the
broken one.

So the prohibition that stood here -- do not "simplify" the fixture to a
relation name `DEFAULT_RELATION_ALIASES` canonicalizes -- is RETIRED, not
relaxed. The configuration it forbade now works, and a warning left standing
would send the next reader hunting a defect that is fixed. The `amount`
constraint above is the one that still binds, and it is unaffected: it is about
which type merges two objects, not about which names resolve.

An earlier draft of this paragraph said `date` never surfaces a `typed_value`
and blamed the type. It had measured only `설립일` declarations, so it read the
alias mismatch as a property of `date`; `date` in fact normalizes every
well-formed input that draft listed -- `2020.03.01`, `2020-03-01`, `2020/3/1`,
`date(2020,3,1)` and `2020.03`, each to 20200301. That record is kept because
the lesson outlived the bug it was about: the variable that draft failed to hold
constant was the LABEL, not the type, and the same confound produced the wrong
severity story in #589's own plan two revisions running.

WHY THE GUARD HAS TO CARRY ITS OWN MARKER, AND CANNOT PIN ITSELF ON THE VALUE.
`policy_defaults.py` defines `DEFAULT_RELATION_ALIASES` and no
`DEFAULT_TYPED_RELATIONS`. When `policy/typed-relations.md` is absent,
`store_typed_relations` returns `{}` -- the normal state of most KBs. So a
typed-relations failure degraded to `{}` renders BYTE-IDENTICALLY to a healthy
KB that declares no typed relations, and no assertion on a rendered number could
tell the two apart. #570's AC-2 argument ("the value itself is wrong, because
the defaults are not the user's rules") does not transfer. What this file
asserts instead is that the degraded page carries a not-computed marker AND the
message naming the file, and every such assertion is paired with a healthy
control that shows the same page carrying the real value.

WHY EVERY FIXTURE HERE PLANTS A HEALTHY `policy/relation-aliases.md`.
#570's guards return before `fact_trust_summary` is ever called, so on a broken
ALIAS file the typed file is never read at all -- measured: alias-ok/typed-broken
is 500 on this tree's parent, alias-broken/typed-broken is 200. A fixture that
left the alias file broken would exercise #570's guard and never reach this one.
`test_both_policy_files_broken_names_one_and_claims_nothing_about_the_other`
covers that interaction on purpose.

SCOPE. Eleven route/method pairs, re-derived rather than copied from #570
(AC-3). All 46 route/method pairs registered on `create_app` outside the
`/static` mount were driven, each with a fresh app and a fresh KB, under five
configurations: both files healthy, typed cp949, typed duplicate alias, alias
malformed, alias cp949. Thirty-nine went through the route sweep; the remaining
seven -- the three `*-unavailable` pages and FastAPI's four built-in doc routes
-- were measured separately, and the `*-unavailable` three answer 409 under
every input, which is their DESIGNED status, so their bodies did run and they
are clean rather than unmeasured. Two cells are named rather than counted clean,
because their handler bodies never ran under any payload I could build:
`POST /settings/root/persist` (control 400) and `POST /settings/test` (control
502). The eleven:
`GET /`, `GET /sources`, `GET /review`, `GET /workbench`, `GET /facts/{id}/row`,
`GET /facts/{id}/provenance`, `POST /facts/{id}/{toggle,accept,reject,amend}`
and `POST /sources/{id}/accept-all`. Four more POSTs under `/sources` are 303s
that only ever landed on 500 by redirecting into `GET /sources`; they are fixed
by guarding that page and have no guard of their own
(`test_the_sources_post_redirects_land_on_a_page_that_survives`).

`POST /ask` and `POST /questions/translate` are not fixed by THIS file's guard.
Both files fail for them at the same statement in
`query_schema.build_query_schema_snapshot`, which `verinote/cli.py` reaches too,
so they are guarded by `query_schema_policy_failure` beside that statement
instead (#591, which #570 deferred); their tests live in
`tests/test_query_schema_policy_guard.py`. `POST /ask` is also the one route in
the population with no upstream guard of any kind, which is why the ordering
rule in `_trust_policy_failure` has nothing above it there.
"""

import re
from pathlib import Path

import ast
import inspect
import pathlib

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from verinote.config import Config
from verinote.pipeline import corroboration
from verinote.store import Store
from verinote.pipeline.corroboration import (
    CorroborationPolicyError,
    typed_relations,
)
from verinote.policy_defaults import RELATION_ALIASES_RELPATH, TYPED_RELATIONS_RELPATH
from verinote.web import create_app

# The bare parser message (G1: it already names its own file, so the guard must
# not wrap it) and the named-file wrapper (G2: `str(UnicodeDecodeError)` is a
# byte offset and no path).
# Quote-free on purpose: Jinja escapes the message's own `'capital'` to
# `&#39;capital&#39;`, so the readable form of this string never appears in a
# rendered page. Mirrors `PARSER_MSG` in the alias guard file.
TYPED_PARSER_MSG = "typed-relations.md: alias"
TYPED_NAMED = f"{TYPED_RELATIONS_RELPATH} could not be read"

TYPED_HEALTHY = "- 자본금: amount as capital\n".encode()
# Raises `CorroborationPolicyError`. See the module docstring: this input was
# chosen when it was the ONLY class that raised here. Since #589 the alias
# file's malformed bytes raise too, so what keeps this fixture honest is the
# MESSAGE its tests assert, not the fact that it raises at all.
TYPED_DUP_ALIAS = "- 자본금: amount as capital\n- 자산: amount as capital\n".encode()
TYPED_CP949 = "- 자본금: amount as capital\n".encode("cp949")
# Parsed to `{}` until #589; now raises with the unparseable-line message, which
# is a DIFFERENT string from `TYPED_DUP_ALIAS`'. Kept out of `BROKEN_INPUTS` on
# purpose: the tests below use it to pin that the two conditions stay
# distinguishable, which is the job the old silent-skip test used to do.
TYPED_TYPO = "- 소속 member_of\n".encode()
TYPED_TYPO_MSG = "typed-relations.md:1: expected"
ALIAS_HEALTHY = "- 소속 -> member_of\n".encode()

# (typed bytes, message that must be present, message that must be absent).
# The "absent" half is the G1/G2 pin #555 measured: deleting the narrow
# `except CorroborationPolicyError` leaves every status unchanged and only swaps
# the bare parser message for the "could not be read" wrapper, so a test written
# as `assert status == 200` does not detect it.
BROKEN_INPUTS = [
    pytest.param(TYPED_DUP_ALIAS, TYPED_PARSER_MSG, TYPED_NAMED, id="duplicate-alias"),
    pytest.param(TYPED_CP949, TYPED_NAMED, TYPED_PARSER_MSG, id="cp949"),
]

# Fact ids in the fixture KB below.
CONFIRMED_AMOUNT = 1  # A / 자본금 / 1억        confirmed, sources/a.txt
REVIEW_AMOUNT = 3  # A / 자본금 / 1억        needs_review, sources/b.txt
REVIEW_PLAIN = 4  # C / 소속   / D          needs_review, sources/a.txt

AMEND_FORM = {
    "subject": "A",
    "relation": "자본금",
    "object": "2억",
    # `_fact_input` accepts only "string" and "term" and rejects anything else
    # before a line of policy-dependent code runs, so a form sending
    # `kind="entity"` measures a 400 and proves nothing about this guard.
    "subject_kind": "string",
    "relation_kind": "string",
    "object_kind": "string",
    "note": "",
}

# Every endpoint that renders a body. `POST /sources/{id}/accept-all` is the
# twelfth member of the population and is absent here on purpose: it answers 303
# with no body, so it gets its own tests below.
BODY_ENDPOINTS = [
    pytest.param("GET", "/", {}, id="dashboard"),
    pytest.param("GET", "/sources", {}, id="sources"),
    pytest.param("GET", "/review", {}, id="review"),
    pytest.param("GET", "/review?filter=corroborated", {}, id="review-filtered"),
    pytest.param("GET", "/workbench", {}, id="workbench"),
    pytest.param("GET", f"/facts/{CONFIRMED_AMOUNT}/row", {}, id="fact-row"),
    pytest.param("GET", f"/facts/{CONFIRMED_AMOUNT}/provenance", {}, id="provenance"),
    pytest.param("POST", f"/facts/{REVIEW_AMOUNT}/toggle", {}, id="toggle"),
    pytest.param("POST", f"/facts/{REVIEW_AMOUNT}/accept", {}, id="accept"),
    pytest.param("POST", f"/facts/{REVIEW_AMOUNT}/reject", {}, id="reject"),
    pytest.param("POST", f"/facts/{REVIEW_AMOUNT}/amend", {"data": AMEND_FORM}, id="amend"),
]


def _done_job(store, source_id: int) -> int:
    """A completed extraction job, so this source's facts clear the
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


def _build(
    tmp_path: Path,
    typed_bytes: bytes | None,
    *,
    alias_bytes: bytes | None = ALIAS_HEALTHY,
    auto_accept: bool = False,
):
    """A KB whose trust signals really move when `policy/typed-relations.md` is
    applied, so that "the value is withheld" is distinguishable from "there was
    never a value".

    `1억` and `100000000원` are the SAME amount and two DIFFERENT strings. With
    the typed declaration `자본금: amount as capital` both normalize to
    `100000000` and the three facts below become one corroborated group backed by
    two distinct sources; without it they are three unrelated raw triples.
    Measured end to end -- with the file vs without it, everything else equal:

        GET /                      "Corroborated review targets" count 1 vs 0
        GET /sources               "1 corroborated" / "2 corroborated" vs "0"
        GET /review                fact 3's chip "corroborated" vs "single source"
        GET /workbench             a corroborated table vs "No facts are ..."
        GET /facts/1/provenance    "corroborated", "2 sources" vs "single source"
        GET /facts/1/row           chip "corroborated" vs "single source"

    That list is what makes every healthy control in this file non-vacuous: a
    guard that fired unconditionally would fail all six.
    """
    root = tmp_path / "kb"
    root.mkdir()
    cfg = Config(
        root=root,
        db_path=root / "kb.sqlite",
        provider="anthropic",
        model="m",
        api_key=None,
        base_url=None,
        auto_accept_recommendations=auto_accept,
    )
    app = create_app(cfg)
    store = app.state.store
    # Real files on disk: `POST /sources/{id}/reanalyze` answers 400 for a source
    # whose bytes are missing, which would make the redirect-landing test measure
    # a validation error instead of the redirect it is about.
    (root / "sources").mkdir(parents=True, exist_ok=True)
    for name in ("a.txt", "b.txt"):
        (root / "sources" / name).write_text("A는 1억이다.\n", encoding="utf-8")
    source_a = store.add_source("sources/a.txt")
    job_a = _done_job(store, source_a)
    source_b = store.add_source("sources/b.txt")
    job_b = _done_job(store, source_b)
    for source_id, name in ((source_a, "a.txt"), (source_b, "b.txt")):
        # `reanalyze` answers 400 without an extracted-text artifact, for the
        # same reason as the files above.
        store.add_source_artifact(
            source_id=source_id, kind="extracted_text", path=f"sources/{name}"
        )
    store.add_fact(
        "A", "자본금", "1억", status="confirmed", confidence=0.9,
        source_id=source_a, job_id=job_a,
    )
    store.add_fact(
        "A", "자본금", "100000000원", status="confirmed", confidence=0.9,
        source_id=source_b, job_id=job_b,
    )
    store.add_fact(
        "A", "자본금", "1억", status="needs_review", confidence=0.5,
        source_id=source_b, job_id=job_b,
    )
    store.add_fact(
        "C", "소속", "D", status="needs_review", confidence=0.5,
        source_id=source_a, job_id=job_a,
    )
    for relpath, data in (
        (RELATION_ALIASES_RELPATH, alias_bytes),
        (TYPED_RELATIONS_RELPATH, typed_bytes),
    ):
        if data is not None:
            path = root / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    return TestClient(app, raise_server_exceptions=False), app


def _client(tmp_path: Path, typed_bytes: bytes | None) -> TestClient:
    return _build(tmp_path, typed_bytes)[0]


@pytest.fixture
def healthy_client(tmp_path: Path) -> TestClient:
    return _client(tmp_path, TYPED_HEALTHY)


@pytest.fixture
def absent_client(tmp_path: Path) -> TestClient:
    return _client(tmp_path, None)


def _request(client: TestClient, method: str, path: str, kwargs: dict):
    return client.request(method, path, follow_redirects=False, **kwargs)


# ---------------------------------------------------------------------------
# AC-1 / AC-5 -- every endpoint in the population survives, and says why
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method, path, kwargs", BODY_ENDPOINTS)
@pytest.mark.parametrize("typed_bytes, present, absent", BROKEN_INPUTS)
def test_every_endpoint_survives_a_broken_typed_relations_file_and_says_why(
    tmp_path, method, path, kwargs, typed_bytes, present, absent
):
    """AC-1 and AC-5 together. Each of these answered 500 on the parent commit.

    The assertion is the MESSAGE, not the status: the narrow
    `except CorroborationPolicyError` (G1) sits above a broad `except Exception`
    (G2) that also returns a string, so deleting G1 leaves every status
    unchanged and only swaps the bare parser message for a "could not be read"
    wrapper -- a claim that a file which WAS read and DID parse could not be
    read at all.
    """
    r = _request(_client(tmp_path, typed_bytes), method, path, kwargs)
    assert r.status_code == 200
    assert present in r.text
    assert absent not in r.text


@pytest.mark.parametrize("method, path, kwargs", BODY_ENDPOINTS)
def test_every_endpoint_is_unchanged_by_a_healthy_typed_relations_file(
    tmp_path, method, path, kwargs
):
    """Anti-vacuity control for the test above: a guard that fired
    unconditionally would put the banner on every healthy KB too."""
    r = _request(_client(tmp_path, TYPED_HEALTHY), method, path, kwargs)
    assert r.status_code == 200
    assert TYPED_PARSER_MSG not in r.text
    assert TYPED_NAMED not in r.text
    assert 'class="error"' not in r.text


@pytest.mark.parametrize("method, path, kwargs", BODY_ENDPOINTS)
def test_an_absent_typed_relations_file_is_not_treated_as_a_failure(
    tmp_path, method, path, kwargs
):
    """`store_typed_relations` returns `{}` and raises nothing when the file does
    not exist, which is the normal state of most KBs. A guard that mistook
    "absent" for "broken" would put a banner on every one of them."""
    r = _request(_client(tmp_path, None), method, path, kwargs)
    assert r.status_code == 200
    assert TYPED_RELATIONS_RELPATH not in r.text
    assert 'class="error"' not in r.text


def test_the_parser_reports_a_typo_instead_of_dropping_it():
    """C-1, at the parser. INVERTED BY #589 -- this test asserted the opposite.

    It used to assert `typed_relations(TYPED_TYPO) == {}`, and its job was to
    keep `TYPED_DUP_ALIAS` from being "simplified" into a silent input that
    would leave the whole file vacuous. That hazard no longer exists: nothing
    non-comment degrades to `{}`.

    The job that remains is to keep the two conditions DISTINGUISHABLE, because
    "both raise" is now the weaker fact. Each is pinned on its own message, so
    swapping one fixture for the other still reddens something.
    """
    with pytest.raises(CorroborationPolicyError) as typo:
        typed_relations(TYPED_TYPO.decode())
    assert TYPED_TYPO_MSG in str(typo.value)

    with pytest.raises(CorroborationPolicyError) as duplicate:
        typed_relations(TYPED_DUP_ALIAS.decode())
    assert "used for both" in str(duplicate.value)

    assert TYPED_TYPO_MSG not in str(duplicate.value)


RAISE_CONDITIONS = {
    "unparseable line":       ("- 소속 member_of\n",              "expected `- name : type as alias`"),
    "unknown type tag":       ("- x: colour as y\n",              "unknown type"),
    "duplicate alias":        ("- 자본금: amount as capital\n- 자산: amount as capital\n", "used for both"),
    "units on a non-`amount` type": ("- x: date as y (원=1)\n",   "units are only valid for amount"),
    "malformed unit pair":    ("- x: amount as y (원)\n",         "malformed unit pair"),
    "non-numeric unit value": ("- x: amount as y (원=abc)\n",     "non-numeric unit value"),
    "invalid unit mapping":   ("- x: amount as y (=5)\n",         "invalid unit mapping"),
}


def _parser_raise_sites() -> list[tuple[str, int]]:
    """Every `raise` reachable from `typed_relations`, transitively, from source.

    Transitive rather than a scan of one function, so a raise added in a NEW
    helper is still counted. Scoped by reachability rather than by grepping the
    filename: `_refuse_canonical_collisions` raises a message beginning
    `typed-relations.md` too, but it is called from `store_typed_relations` and
    is not a parse condition, so a filename grep returns eight and this returns
    the seven that belong to the parser.
    """
    source = inspect.getsource(corroboration)
    tree = ast.parse(source)
    functions = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    seen: set[str] = set()
    stack = ["typed_relations"]
    sites: list[tuple[str, int]] = []
    while stack:
        name = stack.pop()
        if name in seen or name not in functions:
            continue
        seen.add(name)
        for node in ast.walk(functions[name]):
            if isinstance(node, ast.Raise):
                sites.append((name, node.lineno))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                stack.append(node.func.id)
    return sorted(sites, key=lambda site: site[1])


def test_the_raise_conditions_are_derived_from_the_source_not_listed_by_hand():
    """THE TRIPWIRE UNDER THIS FILE'S ENUMERATION, and the reason it exists.

    The count in the module docstring has been wrong twice, and both times it
    was wrong WHEN WRITTEN rather than having gone stale: #585 said four when
    five raised, #589 said six when seven did, omitting `invalid unit mapping`
    both times. Three gates read that paragraph across eight revisions without
    catching it. So the list is not trusted -- it is checked against the source.

    Adding a raise to the parser without adding a case here reddens the count
    assertion; adding a case without naming it in the module docstring reddens
    the last one. That is the closed property underneath an open-looking set:
    the raise SITES are derivable even though the conditions are not.
    """
    sites = _parser_raise_sites()
    assert len(sites) == len(RAISE_CONDITIONS), (
        f"{len(sites)} raise sites reachable from `typed_relations` but "
        f"{len(RAISE_CONDITIONS)} conditions listed here: {sites}"
    )
    # Whitespace-normalised: the docstring wraps the list across lines, and the
    # claim under test is that it NAMES each condition, not how it wraps them.
    prose = " ".join(__doc__.split())
    for label, (text, needle) in RAISE_CONDITIONS.items():
        with pytest.raises(CorroborationPolicyError) as excinfo:
            typed_relations(text)
        assert needle in str(excinfo.value), label
        assert label in prose, f"the module docstring no longer names {label!r}"


def test_an_unknown_type_tag_is_reported_not_skipped():
    """#589's SECOND raise, pinned apart from the first on purpose.

    `- x: colour as y` MATCHES `_TYPED_REL_RE`; only the type tag is wrong. So
    it is refused by a different branch than an unparseable line, and reverting
    either branch alone leaves the other one raising. Asserted in its own test
    because the matrix showed that folding it in with the unparseable case left
    this branch pinned by nothing: every test that covered it covered the other
    branch too, so deleting this raise reddened only tests that would have gone
    red anyway.

    Matching the shape is also why refusing it is safe: a line this close to a
    declaration cannot be mistaken for prose the user never meant as one.
    """
    with pytest.raises(CorroborationPolicyError) as excinfo:
        typed_relations("- 설립일: colour as founded_on\n")
    message = str(excinfo.value)
    assert "unknown type 'colour'" in message
    assert TYPED_TYPO_MSG not in message


def test_comments_and_blanks_and_markdown_headings_still_parse():
    """The tolerance half of #589's rule, which is what makes it the SIBLING's
    rule rather than a stricter one invented here. A parser that raised on
    these would break every policy file with a heading in it."""
    assert typed_relations("## Types\n\n# a note\n") == {}
    assert typed_relations("- 자본금: amount as capital  # trailing note\n") != {}


@pytest.mark.parametrize("method, path, kwargs", BODY_ENDPOINTS)
def test_a_typod_declaration_now_renders_the_banner_it_used_to_suppress(
    tmp_path, method, path, kwargs
):
    """The page half, INVERTED BY #589 -- it asserted no banner on every page.

    The old rationale was that reporting a dropped declaration through THIS
    guard would mean claiming a file that was read and parsed could not be read.
    #589 removed the premise rather than the objection: the file no longer
    parses, so the banner is not a false claim about it -- it names a line the
    parser refused. The page still answers 200, which is the part of #585's
    guarantee that must survive a change like this.
    """
    r = _request(_client(tmp_path, TYPED_TYPO), method, path, kwargs)
    assert r.status_code == 200
    assert TYPED_TYPO_MSG in r.text
    assert TYPED_NAMED not in r.text


# ---------------------------------------------------------------------------
# AC-2 -- withheld, never substituted. Each page's own typed-derived value.
# ---------------------------------------------------------------------------

DASHBOARD_CORROBORATED_ROW = re.compile(
    r"Corroborated review targets.*?<td class=\"conf\">(.*?)</td>", re.S
)
NOT_COMPUTED = '<span class="badge muted">not computed</span>'
ROW_NOT_COMPUTED = '<span class="badge muted">trust not computed</span>'
DOSSIER_NOT_COMPUTED = "Not computed — see the policy-file notice above."
WORKBENCH_NO_CORROBORATION = "No facts are corroborated by multiple distinct sources."


def _dashboard_corroborated(body: str) -> str:
    match = DASHBOARD_CORROBORATED_ROW.search(body)
    assert match is not None, "the queue row this test is about is not on the page"
    return match.group(1).strip()


@pytest.mark.parametrize("typed_bytes, present, absent", BROKEN_INPUTS)
def test_the_dashboard_withholds_the_count_the_typed_file_would_have_changed(
    tmp_path, typed_bytes, present, absent
):
    """`Corroborated review targets` is 1 under this KB's typed file and 0
    without it, so a page that printed either number would be making a claim it
    could not compute -- and `0` is the one a naive `{}` fallback produces."""
    del present, absent
    body = _client(tmp_path, typed_bytes).get("/").text
    assert _dashboard_corroborated(body) == NOT_COMPUTED


def test_a_healthy_typed_file_gives_the_dashboard_its_real_count(healthy_client):
    assert _dashboard_corroborated(healthy_client.get("/").text) == "1"


def test_the_absent_file_build_gives_a_different_count_than_the_healthy_one(
    absent_client,
):
    """The measurement the two tests above rest on: the withheld value is a
    value that really does depend on this file. Without it this page's AC-2
    assertion would be pinning a constant."""
    assert _dashboard_corroborated(absent_client.get("/").text) == "0"


@pytest.mark.parametrize("typed_bytes, present, absent", BROKEN_INPUTS)
def test_sources_withholds_the_trust_counts_the_typed_file_would_have_changed(
    tmp_path, typed_bytes, present, absent
):
    del present, absent
    body = _client(tmp_path, typed_bytes).get("/sources").text
    assert body.count(ROW_NOT_COMPUTED) == 2
    assert "corroborated</span>" not in body


def test_a_healthy_typed_file_gives_sources_its_real_trust_counts(healthy_client):
    body = healthy_client.get("/sources").text
    assert ROW_NOT_COMPUTED not in body
    assert '<span class="badge trust-corroborated">1 corroborated</span>' in body
    assert '<span class="badge trust-corroborated">2 corroborated</span>' in body


def test_sources_counts_differ_without_the_typed_file(absent_client):
    """`0 corroborated` is what a `{}` fallback would render, and it is a
    legitimate rendering for a KB with no typed declarations -- which is exactly
    why the degraded page must not render a number at all."""
    body = absent_client.get("/sources").text
    assert body.count('<span class="badge trust-corroborated">0 corroborated</span>') == 2


@pytest.mark.parametrize("typed_bytes, present, absent", BROKEN_INPUTS)
def test_the_review_queue_withholds_each_row_s_typed_derived_signals(
    tmp_path, typed_bytes, present, absent
):
    del present, absent
    body = _client(tmp_path, typed_bytes).get("/review").text
    assert body.count(ROW_NOT_COMPUTED) == 2
    assert '<span class="badge chip">corroborated</span>' not in body
    assert '<span class="badge chip">single source</span>' not in body


def test_a_healthy_typed_file_gives_the_review_queue_its_real_chips(healthy_client):
    body = healthy_client.get("/review").text
    assert ROW_NOT_COMPUTED not in body
    assert '<span class="badge chip">corroborated</span>' in body


def test_the_review_queue_chip_differs_without_the_typed_file(absent_client):
    body = absent_client.get("/review").text
    assert '<span class="badge chip">corroborated</span>' not in body
    assert '<span class="badge chip">single source</span>' in body


@pytest.mark.parametrize("typed_bytes, present, absent", BROKEN_INPUTS)
def test_the_workbench_withholds_both_tables(tmp_path, typed_bytes, present, absent):
    del present, absent
    body = _client(tmp_path, typed_bytes).get("/workbench").text
    assert body.count(DOSSIER_NOT_COMPUTED) == 2
    assert WORKBENCH_NO_CORROBORATION not in body


def test_a_healthy_typed_file_gives_the_workbench_its_corroborated_group(
    healthy_client,
):
    body = healthy_client.get("/workbench").text
    assert DOSSIER_NOT_COMPUTED not in body
    assert WORKBENCH_NO_CORROBORATION not in body
    assert "<code>자본금</code>" in body


def test_the_workbench_has_no_corroborated_group_without_the_typed_file(
    absent_client,
):
    assert WORKBENCH_NO_CORROBORATION in absent_client.get("/workbench").text


@pytest.mark.parametrize("typed_bytes, present, absent", BROKEN_INPUTS)
def test_the_dossier_withholds_the_typed_value_row(
    tmp_path, typed_bytes, present, absent
):
    del present, absent
    body = _client(tmp_path, typed_bytes).get(f"/facts/{CONFIRMED_AMOUNT}/provenance").text
    assert DOSSIER_NOT_COMPUTED in body
    assert "as <code>capital</code>" not in body
    # The fact's own identity is not withheld with its trust dossier.
    assert '<span class="rel term-string">&#34;자본금&#34;' in body


def test_a_healthy_typed_file_gives_the_dossier_its_typed_value_row(healthy_client):
    body = healthy_client.get(f"/facts/{CONFIRMED_AMOUNT}/provenance").text
    assert DOSSIER_NOT_COMPUTED not in body
    assert "as <code>capital</code>" in body
    assert "2 sources" in body


def test_the_dossier_has_no_typed_value_row_without_the_typed_file(absent_client):
    body = absent_client.get(f"/facts/{CONFIRMED_AMOUNT}/provenance").text
    assert "as <code>capital</code>" not in body
    assert "1 source" in body


@pytest.mark.parametrize("typed_bytes, present, absent", BROKEN_INPUTS)
def test_the_fact_row_withholds_its_typed_chip(tmp_path, typed_bytes, present, absent):
    del present, absent
    body = _client(tmp_path, typed_bytes).get(f"/facts/{CONFIRMED_AMOUNT}/row").text
    assert ROW_NOT_COMPUTED in body
    # The chip markup, not the bare word: the duplicate-alias message
    # legitimately contains "capital", so `"capital" not in body` would be
    # reddened by the very message this guard is supposed to render.
    assert '<span class="badge chip">amount <code>capital</code></span>' not in body
    # Withholding trust must not withhold the fact or its controls.
    assert f'id="fact-{CONFIRMED_AMOUNT}"' in body
    assert f'hx-post="/facts/{CONFIRMED_AMOUNT}/toggle"' in body


TYPED_CHIP = '<span class="badge chip">amount <code>capital</code></span>'


def test_a_healthy_typed_file_gives_the_fact_row_its_typed_chip(healthy_client):
    body = healthy_client.get(f"/facts/{CONFIRMED_AMOUNT}/row").text
    assert ROW_NOT_COMPUTED not in body
    assert TYPED_CHIP in body


def test_the_fact_row_has_no_typed_chip_without_the_typed_file(absent_client):
    assert TYPED_CHIP not in absent_client.get(f"/facts/{CONFIRMED_AMOUNT}/row").text


# ---------------------------------------------------------------------------
# The write path: POST /sources/{id}/accept-all, and the auto-accept pass
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("typed_bytes, present, absent", BROKEN_INPUTS)
def test_accept_all_survives_a_broken_typed_file_when_auto_accept_is_on(
    tmp_path, typed_bytes, present, absent
):
    """The one endpoint in the population invisible to a sweep that leaves
    `auto_accept_recommendations` at its `False` default: it answers 303 either
    way with the flag off, and 500 with the flag on. It never enters
    `_fact_row_context`, so no template guard's deletion touches it.

    The assertion is a status and not a message, for the only reason this file
    accepts one: a 303 carries no body to put a message in (measured).
    """
    del present, absent
    client = _build(tmp_path, typed_bytes, auto_accept=True)[0]
    r = client.post("/sources/1/accept-all", follow_redirects=False)
    assert r.status_code == 303


@pytest.mark.parametrize("typed_bytes, present, absent", BROKEN_INPUTS)
def test_a_broken_typed_file_stops_the_auto_accept_pass_rather_than_running_it_empty(
    tmp_path, typed_bytes, present, absent
):
    """The strongest form of AC-2, because this one is a WRITE.
    `apply_auto_accept_recommendations` promotes facts to `accepted`, and
    `acceptance._engine` decides which by reading the alias file and the typed
    file on consecutive lines. A badge computed on the wrong rules is re-rendered
    next request; a status transition is committed and audited.

    Fact 3 is promoted here only because `자본금: amount as capital` makes `1억`
    and `100000000원` one normalized value backed by two distinct sources -- the
    healthy half below is what proves this fixture can see a promotion at all.

    STATED LIMIT: "fact 3 is still needs_review" is also what a pass run with no
    typed file at all produces, and unlike #570's alias case that build IS
    reachable here, because an absent typed file is legal. So this assertion
    does not by itself separate "withheld" from "degraded to `{}`" -- nothing
    rendered or written could, which is the point of the module docstring's
    third paragraph. Its falsifying build is the guard's deletion, which 500s
    the request.
    """
    del present, absent

    (tmp_path / "healthy").mkdir()
    (tmp_path / "broken").mkdir()
    healthy, healthy_app = _build(
        tmp_path / "healthy", TYPED_HEALTHY, auto_accept=True
    )
    assert healthy.post(f"/facts/{REVIEW_PLAIN}/accept").status_code == 200
    assert healthy_app.state.store.get_fact(REVIEW_AMOUNT)["status"] == "accepted"

    broken, broken_app = _build(tmp_path / "broken", typed_bytes, auto_accept=True)
    r = broken.post(f"/facts/{REVIEW_PLAIN}/accept")
    assert r.status_code == 200
    assert broken_app.state.store.get_fact(REVIEW_AMOUNT)["status"] == "needs_review"


@pytest.mark.parametrize("typed_bytes, present, absent", BROKEN_INPUTS)
def test_the_sources_post_redirects_land_on_a_page_that_survives(
    tmp_path, typed_bytes, present, absent
):
    """C-3. These four POSTs answer 303 under every input, on the parent commit
    too -- they read no policy file. They appeared in the issue's population only
    because its sweep followed the redirect into `GET /sources`, which really was
    500. Guarding that page is what fixes them, and this test is the only thing
    that would notice if `GET /sources` were ever guarded differently.
    """
    for index, path in enumerate(("/sources/1/reanalyze", "/sources/1/delete")):
        # A fresh KB per path: `delete` removes the source the next POST would
        # need, and a shared client would measure a 404 rather than a redirect.
        endpoint_dir = tmp_path / f"endpoint{index}"
        endpoint_dir.mkdir()
        landing_dir = tmp_path / f"landing{index}"
        landing_dir.mkdir()
        endpoint = _client(endpoint_dir, typed_bytes).post(path, follow_redirects=False)
        assert endpoint.status_code == 303
        assert endpoint.headers["location"] == "/sources"
        landing = _client(landing_dir, typed_bytes).post(path, follow_redirects=True)
        assert landing.status_code == 200
        assert present in landing.text
        assert absent not in landing.text


# ---------------------------------------------------------------------------
# The interaction with #570: two broken files, and the order they are reported
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "alias_bytes, alias_marker",
    [
        pytest.param("- 소속 member_of\n".encode(), "relation-aliases.md:1:", id="alias-malformed"),
        pytest.param("- 소속 -> member_of\n".encode("cp949"), RELATION_ALIASES_RELPATH, id="alias-cp949"),
    ],
)
def test_both_policy_files_broken_names_one_and_claims_nothing_about_the_other(
    tmp_path, alias_bytes, alias_marker
):
    """`_trust_policy_failure` checks the alias file first and returns on the
    first failure, so with both files broken the page names
    `relation-aliases.md` and says nothing at all about `typed-relations.md`.

    What this test really pins is the SECOND half: nothing on the degraded page
    asserts that the file it does not name is healthy. That sentence used to
    exist -- the banners said the values "would have been computed under
    different ALIAS RULES than this KB's file specifies", which on a KB with two
    broken files named the wrong cause.

    THE CP949 ARM COVERS THE ORDERING, NOT THE TOLERANCE CLAUSE -- and saying so
    matters, because it was added believing it did the latter. Both arms pass
    whether `store_typed_relations`' alias-read tolerance catches one exception
    class or all of them, MEASURED by narrowing it back and re-running: 99
    passed either way. The reason is this route: `_trust_policy_failure` reads
    the alias file FIRST and returns, so the page never reaches
    `store_typed_relations` at all. Every page-level probe inherits that
    pre-check, so no test driven through a route can pin what happens when the
    typed guard runs without it. `test_the_typed_guard_never_names_its_own_file_for_an_alias_failure`
    below is the pin that can, and it is deliberately NOT a route test.
    """
    body = _build(tmp_path, TYPED_DUP_ALIAS, alias_bytes=alias_bytes)[0].get("/sources").text
    assert alias_marker in body
    assert TYPED_RELATIONS_RELPATH not in body
    assert "alias rules" not in body


def _own_calls(node: ast.AST, name: str) -> list[int]:
    """Calls to ``name`` in this function's OWN body, excluding nested ``def``s.

    Excluding the nested spans is the whole point. `ast.walk` descends into
    inner functions, so a scan that does not exclude them credits an OUTER
    function with its inner one's calls -- which is exactly how the comment
    this test replaces came to say eight callers when there are seven. The
    eighth was `create_app`, which does not call it; `_source_trust_rollup`,
    nested inside it, does.

    A `lambda` is deliberately NOT excluded, and the distinction is load-bearing
    here rather than pedantic: `query_schema_policy_failure` passes
    `lambda: store_typed_relations(store)` to the guard, and that lambda runs in
    its enclosing function's scope, on that function's alias read. Crediting it
    to the enclosing `def` is what makes the property mean what it says. A
    nested `def` is a separate callable that could be invoked from anywhere, so
    it is counted on its own.
    """
    nested = [
        (inner.lineno, inner.end_lineno)
        for inner in ast.walk(node)
        if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)) and inner is not node
    ]
    return [
        sub.lineno
        for sub in ast.walk(node)
        if isinstance(sub, ast.Call)
        and getattr(sub.func, "id", None) == name
        and not any(start <= sub.lineno <= end for start, end in nested)
    ]


def test_every_typed_reader_reads_the_alias_file_itself():
    """#589. The property that licenses `store_typed_relations`' broad `except`.

    That function reads the alias file to refuse canonical collisions, and
    swallows ANY failure of that read rather than reporting the alias file's
    problem under the typed file's name. Swallowing is safe only because every
    caller reads the alias file itself and will report the failure against the
    right file -- so this checks that, rather than trusting a sentence.

    Derived, not listed: the caller set comes from the source, so a NEW caller
    that skipped the alias read reddens here instead of quietly making the
    comment in `corroboration.py` false.
    """
    package = pathlib.Path(corroboration.__file__).parent.parent
    callers: list[tuple[str, bool]] = []
    for path in sorted(package.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "store_typed_relations(" not in source:
            continue
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name == "store_typed_relations":
                continue
            if not _own_calls(node, "store_typed_relations"):
                continue
            callers.append(
                (f"{path.relative_to(package)}::{node.name}",
                 bool(_own_calls(node, "store_relation_aliases")))
            )

    assert callers, "no callers found -- the derivation itself is broken"
    offenders = [name for name, reads_alias in callers if not reads_alias]
    assert not offenders, (
        "these read the typed file without reading the alias file, so a "
        f"swallowed alias failure would go unreported: {offenders}"
    )


def test_the_typed_guard_never_names_its_own_file_for_an_alias_failure(tmp_path):
    """#589. The typed guard applied WITHOUT an alias pre-check ahead of it.

    `store_typed_relations` reads the alias file since #589, to refuse two
    declarations that canonicalise to one relation. So it can now fail for a
    reason that belongs to the OTHER file, and a guard wrapping it would name
    `typed-relations.md` for an alias problem -- with a byte offset from a file
    it is not talking about.

    ITS TOLERANCE CLAUSE MUST CATCH EVERY CLASS, not just the policy one. A
    malformed alias file raises `CorroborationPolicyError`; a cp949 one raises
    `UnicodeDecodeError`, a SIBLING of `ValueError` and not a policy error at
    all. The narrow form let the second through, and this test reddens on the
    cp949 row under it -- measured, which is the whole reason it exists rather
    than a parametrized arm on a route test: every route reads the alias file
    first, so no page can reach this.

    The healthy row is the anti-vacuity control: it proves the guard returns
    `None` because nothing failed, not because the assertion cannot fire.
    """
    for label, alias_bytes in (
        ("malformed", "- 소속 member_of\n".encode()),
        ("cp949", "- 소속 -> member_of\n".encode("cp949")),
        ("healthy", "- 소속 -> member_of\n".encode()),
    ):
        root = tmp_path / label
        policy = root / "policy"
        policy.mkdir(parents=True)
        (policy / "relation-aliases.md").write_bytes(alias_bytes)
        (policy / TYPED_RELATIONS_RELPATH.split("/")[-1]).write_text(
            "- 자본금 : amount as capital\n", encoding="utf-8"
        )
        store = Store(root / "kb.sqlite")
        store.init_schema()
        failure = corroboration.policy_file_failure(
            lambda: corroboration.store_typed_relations(store),
            TYPED_RELATIONS_RELPATH,
        )
        assert failure is None, f"{label}: typed guard reported {failure!r}"


def test_repairing_the_alias_file_reveals_the_typed_failure_without_a_500(tmp_path):
    """The both-broken case is masked on the parent commit: #570's guard returns
    before `fact_trust_summary` runs, so the typed file is never read and the
    page answers 200 with an alias-only banner. Repairing the alias file used to
    turn that page into a fresh 500. It now turns into a second banner.
    """
    client, app = _build(
        tmp_path, TYPED_DUP_ALIAS, alias_bytes="- 소속 member_of\n".encode()
    )
    first = client.get("/sources")
    assert first.status_code == 200
    assert "relation-aliases.md:1:" in first.text

    (app.state.cfg.root / RELATION_ALIASES_RELPATH).write_bytes(ALIAS_HEALTHY)

    second = client.get("/sources")
    assert second.status_code == 200
    assert TYPED_PARSER_MSG in second.text


def test_a_broken_alias_file_still_degrades_exactly_as_before(tmp_path):
    """The rename from `alias_error` to `policy_error` is mechanical, and this is
    where a silent behaviour change in it would show: a healthy typed file plus a
    broken alias file must still be the #570 story, message and all."""
    client = _client(tmp_path, TYPED_HEALTHY)
    (client.app.state.cfg.root / RELATION_ALIASES_RELPATH).write_bytes(
        "- 소속 member_of\n".encode()
    )
    body = client.get("/sources").text
    assert "relation-aliases.md:1:" in body
    assert f"{RELATION_ALIASES_RELPATH} could not be read" not in body
    assert ROW_NOT_COMPUTED in body


# ---------------------------------------------------------------------------
# The prose: one guard per template, one test per template
# ---------------------------------------------------------------------------

# The exact phrases that were true of the alias file and false of this one.
# NOT a bare `"alias" not in body`: the typed parser's own message legitimately
# contains the word ("typed-relations.md: alias 'capital' used for both ..."),
# so that assertion would be reddened by the thing it is meant to allow.
OLD_BANNER_PHRASE = "different alias rules"
NEW_BANNER_PHRASE = "different rules than this KB's policy files specify"
OLD_MARKER = "see the alias-file notice above"
NEW_MARKER = "see the policy-file notice above"
# C-7. `settings.html` has no typed-relations editor, and `docs/operations.md`
# records that nothing in verinote writes this file -- so a banner about it that
# ends "Fix it on Settings" points at a page that cannot repair it.
SETTINGS_REPAIR_LINK = 'Fix it on <a href="/settings">Settings</a>'

BANNER_RE = re.compile(r'<p class="error" role="alert">.*?</p>', re.S)


def _collapse(text: str) -> str:
    """Collapse runs of whitespace. These sentences are wrapped across source
    lines, so the newline falls in a different place in each template and a
    literal substring match would pin the line wrapping rather than the wording.
    """
    return re.sub(r"\s+", " ", text)


def _banner(body: str) -> str:
    """This page's own policy banner, so that reverting ANOTHER template's
    sentence cannot redden this page's test. Within one template the sites are
    one guard and are declared as such: the three `dashboard.html` markers and
    the five in `provenance.html` render the same string, so no assertion tells
    them apart."""
    banners = BANNER_RE.findall(body)
    assert len(banners) == 1, f"expected exactly one policy banner, found {len(banners)}"
    return banners[0]


def _assert_banner_prose(body: str) -> None:
    banner = _collapse(_banner(body))
    assert TYPED_PARSER_MSG in banner
    assert OLD_BANNER_PHRASE not in banner
    assert NEW_BANNER_PHRASE in banner
    assert SETTINGS_REPAIR_LINK not in banner


def test_the_dashboard_banner_names_no_file_it_did_not_read(tmp_path):
    body = _client(tmp_path, TYPED_DUP_ALIAS).get("/").text
    _assert_banner_prose(body)
    assert OLD_MARKER not in body
    # Six: `dashboard.html` has three marker sites, and one of them is the
    # `title=` on a suppressed Open button inside the queue loop, which renders
    # once per policy-dependent row (four of them).
    assert body.count(NEW_MARKER) == 6


def test_the_sources_banner_names_no_file_it_did_not_read(tmp_path):
    _assert_banner_prose(_client(tmp_path, TYPED_DUP_ALIAS).get("/sources").text)


def test_the_review_banner_names_no_file_it_did_not_read(tmp_path):
    _assert_banner_prose(_client(tmp_path, TYPED_DUP_ALIAS).get("/review").text)


def test_the_workbench_banner_names_no_file_it_did_not_read(tmp_path):
    body = _client(tmp_path, TYPED_DUP_ALIAS).get("/workbench").text
    _assert_banner_prose(body)
    assert OLD_MARKER not in body
    assert body.count(NEW_MARKER) == 2


def test_the_provenance_banner_names_no_file_it_did_not_read(tmp_path):
    body = _client(tmp_path, TYPED_DUP_ALIAS).get(
        f"/facts/{CONFIRMED_AMOUNT}/provenance"
    ).text
    _assert_banner_prose(body)
    assert OLD_MARKER not in body
    assert body.count(NEW_MARKER) == 5


def test_the_fact_row_reason_line_offers_no_repair_it_cannot_deliver(tmp_path):
    """The sixth prose site, and the only one on a fragment rather than a page.
    `fact_row.html`'s sentence was already file-agnostic ("withheld until this
    file can be read"); its Settings link was not. Asserted on
    `GET /facts/{id}/row`, which renders this template and nothing else, so
    reverting any page banner leaves this test green."""
    body = _client(tmp_path, TYPED_DUP_ALIAS).get(f"/facts/{CONFIRMED_AMOUNT}/row").text
    assert BANNER_RE.search(body) is None
    assert "withheld until this file can be read." in body
    assert SETTINGS_REPAIR_LINK not in body
