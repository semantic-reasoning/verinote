# SPDX-License-Identifier: MPL-2.0
"""#589 path 2: a typed declaration written under an ALIASED label takes effect.

`typed_relations` keys its dict by the label the user WROTE. Every consumer
looks the spec up by the CANONICAL label. So a declaration on any label the
alias table rewrites was stored under a key nobody queries, and did nothing --
with no error, no warning, and nothing distinguishable in any rendered value.

WHY THE FIXTURE USES `설립일` AND NOT AN INVENTED LABEL. It is one of the raw
keys in the PACKAGED `DEFAULT_RELATION_ALIASES`, so this reproduces on a default
install with no user alias file at all. Every test here pairs it with `자본금`,
which that table leaves alone, DECLARED AT THE SAME TYPE -- holding the type
constant is the control the original #585 diagnosis lacked, which is how the
defect got misreported as "the `date` type never surfaces a typed value".

WHY `established_on` MUST BE FUNCTIONAL HERE. The conflict half of the harm only
appears when the canonical relation is single-valued. On a default install the
functional relations are exactly the ones a user names in their own words, so
this is the represented case rather than a corner.

THE BASELINE ARM IS LOAD-BEARING, not decoration. Before the fix the aliased arm
was byte-identical to having NO typed file at all -- measured, cell for cell. So
a test that compares "declared under an alias" against "declared canonically"
cannot tell a working remedy from a default; it needs the no-file arm to show
that the numbers it asserts are the REMEDY's and not the default's. `A3` carries
that arm for exactly this reason.
"""
import pytest

from fastapi.testclient import TestClient

from verinote.config import Config
from verinote.pipeline.acceptance import _engine
from verinote.pipeline.corroboration import (
    CorroborationPolicyError,
    relation_aliases,
    store_relation_aliases,
    store_single_valued_conflicts,
    store_typed_relations,
    typed_spec_for_canonical,
)
from verinote.pipeline.trust import fact_trust_summary
from verinote.pipeline.workbench import trust_workbench
from verinote.policy_defaults import DEFAULT_RELATION_ALIASES
from verinote.store import Store
from verinote.web.app import create_app

ALIASED = "설립일"           # a raw key in the PACKAGED table -> established_on
CANONICAL = "established_on"
UNALIASED = "자본금"          # absent from that table; canonicalises to itself
SIBLING = "창립일"            # a SECOND raw key routing to established_on
V1, V2 = "1976.04.01", "date(1976,4,1)"   # one date, two notations


def _decl(relation: str, alias: str = "founded") -> str:
    # The alias TAG must differ per declaration: `typed-relations.md` already
    # refuses one tag used for two relations, and that refusal is not what any
    # test here is about.
    return f"- {relation} : date as {alias}\n"


def _seed(tmp_path, typed: str | None, *, relation: str, functional: str):
    policy = tmp_path / "policy"
    policy.mkdir(parents=True)
    (policy / "logic-policy.dl").write_text(
        f'.decl functional(rel: symbol)\nfunctional("{functional}").\n', encoding="utf-8"
    )
    # No user relation-aliases.md on purpose: the packaged defaults are the
    # population the defect actually hits.
    if typed is not None:
        (policy / "typed-relations.md").write_text(typed, encoding="utf-8")
    return relation


def _store(tmp_path, typed: str | None, *, relation=ALIASED, functional=CANONICAL):
    _seed(tmp_path, typed, relation=relation, functional=functional)
    store = Store(tmp_path / "kb.sqlite")
    store.init_schema()
    a = store.add_source("sources/a.txt")
    b = store.add_source("sources/b.txt")
    store.add_fact("Acme", relation, V1, status="confirmed", source_id=a)
    store.add_fact("Acme", relation, V2, status="confirmed", source_id=b)
    return store


def _client(tmp_path, typed: str | None, *, relation=ALIASED, functional=CANONICAL):
    _seed(tmp_path, typed, relation=relation, functional=functional)
    cfg = Config(
        root=tmp_path, db_path=tmp_path / "kb.sqlite",
        provider="anthropic", model="m", api_key=None, base_url=None,
    )
    app = create_app(cfg)
    client = TestClient(app)
    store = app.state.store
    a = store.add_source("sources/a.txt")
    b = store.add_source("sources/b.txt")
    store.add_fact("Acme", relation, V1, status="confirmed", source_id=a)
    store.add_fact("Acme", relation, V2, status="confirmed", source_id=b)
    return client


def _badges(body: str, name: str) -> list[int]:
    import re
    return [int(x) for x in re.findall(rf"{name}[^0-9]{{0,40}}(\d+)", body)]


def test_the_fixture_label_is_one_the_packaged_table_rewrites(tmp_path):
    """Anti-vacuity for every test below, and the exact check #585 skipped.

    If `설립일` ever stops being a packaged raw key, or `자본금` starts being
    one, the pairing stops being a controlled comparison and every assertion
    below would still pass while proving nothing about aliased declarations.
    """
    aliases = relation_aliases(DEFAULT_RELATION_ALIASES)
    assert aliases.get(ALIASED) == CANONICAL
    assert aliases.get(SIBLING) == CANONICAL
    assert UNALIASED not in aliases


def test_an_aliased_declaration_resolves(tmp_path):
    """A1 / G1. The resolver and the dossier row, at the same type as the control."""
    store = _store(tmp_path, _decl(ALIASED))
    typed = store_typed_relations(store)
    aliases = store_relation_aliases(store)

    assert list(typed) == [ALIASED], "the dict still keys by the written label"
    spec = typed_spec_for_canonical(typed, CANONICAL, aliases)
    assert spec is not None and spec.type == "date"

    fact_id = next(int(r["id"]) for r in store.facts())
    summary = fact_trust_summary(store, fact_id)
    assert summary.typed_value is not None
    assert summary.typed_value.normalized_value == 19760401


def test_an_unaliased_declaration_still_resolves(tmp_path):
    """A4. The control arm: same type, a label the packaged table leaves alone."""
    store = _store(tmp_path, _decl(UNALIASED), relation=UNALIASED, functional=UNALIASED)
    typed = store_typed_relations(store)
    aliases = store_relation_aliases(store)
    spec = typed_spec_for_canonical(typed, UNALIASED, aliases)
    assert spec is not None and spec.type == "date"

    fact_id = next(int(r["id"]) for r in store.facts())
    summary = fact_trust_summary(store, fact_id)
    assert summary.typed_value is not None
    assert summary.typed_value.normalized_value == 19760401


def test_two_agreeing_sources_corroborate_under_an_aliased_declaration(tmp_path):
    """A2 / G2. `trust_workbench` -- 0 before the fix, and NOT via `trust.py`.

    This number is formed by `workbench.py`'s own spec lookup, not by the one in
    `trust.py`: patching `trust.py` alone leaves it at 0. Measured that way, per
    site, which is the only thing that could have shown it.
    """
    store = _store(tmp_path, _decl(ALIASED))
    workbench = trust_workbench(store)
    assert len(workbench.corroborated) == 1
    assert workbench.corroborated[0].source_count == 2
    assert workbench.conflicts == ()


def test_a_typed_declaration_resolves_the_notation_conflict(tmp_path):
    """A3 / G4. `single_valued_conflicts` -- 1 before the fix."""
    store = _store(tmp_path, _decl(ALIASED))
    assert store_single_valued_conflicts(store) == []


def test_the_sources_rollup_stops_reporting_the_false_conflict(tmp_path):
    """A3 / G4 + G5, at the rendered surface, WITH the baseline arm.

    The two badges come from different sites -- `conflicted` from
    `store_single_valued_conflicts`, `corroborated` from `_source_object_key` --
    so both are asserted. The no-typed-file arm is what makes this test able to
    fail for the right reason: before the fix the declared arm was identical to
    it, so asserting the declared arm alone could not tell a working remedy from
    the default.
    """
    declared = _client(tmp_path / "declared", _decl(ALIASED)).get("/sources").text
    assert _badges(declared, "conflicted") == [0, 0]
    assert _badges(declared, "corroborated") == [1, 1]

    baseline = _client(tmp_path / "baseline", None).get("/sources").text
    assert _badges(baseline, "conflicted") == [1, 1]
    assert _badges(baseline, "corroborated") == [0, 0]


def test_the_rollup_corroborates_an_aliased_declaration_with_no_conflict(tmp_path):
    """G5 alone. `_source_object_key`, isolated from `single_valued_conflicts`.

    WITHOUT THIS TEST G5 IS UNPINNED, which the matrix showed: its only red was
    a test `G4` reddens too, so reverting `_source_object_key` alone would have
    been caught by nothing of its own. The two are separated here by making the
    canonical relation NON-functional -- `single_valued_conflicts` then returns
    nothing whatever the typed lookup does, so `conflicted` cannot move and the
    `corroborated` badge is left as the only thing under test.

    This is also the plan's own §1.3 caveat, which was otherwise unpinned: on a
    non-functional relation the aliased declaration still silently fails, it
    just fails quietly -- corroboration is lost with no conflict raised to show
    that anything went wrong.
    """
    client = _client(tmp_path, _decl(ALIASED), functional="unrelated_relation")
    body = client.get("/sources").text
    assert _badges(body, "conflicted") == [0, 0]
    assert _badges(body, "corroborated") == [1, 1]


def test_the_acceptance_view_resolves_an_aliased_declaration(tmp_path):
    """G3. `acceptance._view_fact` -- the fifth site, and the one with no
    consumer in this issue's harm story, so nothing else here would pin it."""
    store = _store(tmp_path, _decl(ALIASED))
    engine = _engine(store)
    keys = {f.object_key for f in engine.facts}
    assert keys == {("scalar", 19760401)}, keys


def test_two_declarations_canonicalising_to_one_relation_are_refused(tmp_path):
    """A5 / G4-collision. Ambiguity the fix creates, refused rather than ordered.

    Before this change these were separate keys and BOTH were ignored, so there
    was nothing to disambiguate. Resolving them makes dict order decide which
    declaration wins -- the defect class this change exists to remove -- so it
    raises the way `typed-relations.md` already refuses a duplicate alias.

    IT RAISES FROM `store_typed_relations`, NOT FROM THE RESOLVER, and the test
    below this one is what pins that distinction. Raising from the resolver put
    the error below every guard the trust path has.
    """
    store = _store(tmp_path, _decl(ALIASED) + _decl(SIBLING, "incorporated"))
    with pytest.raises(CorroborationPolicyError) as excinfo:
        store_typed_relations(store)
    message = str(excinfo.value)
    assert ALIASED in message
    assert SIBLING in message
    assert CANONICAL in message


@pytest.mark.parametrize(
    "route", ["/", "/sources", "/review", "/workbench", "/report", "/questions"]
)
def test_a_collision_degrades_every_page_instead_of_500ing_it(tmp_path, route):
    """THE REGRESSION GUARD, and the reason the refusal lives where it does.

    An earlier revision refused the collision inside the resolver. That made the
    `CorroborationPolicyError` a NEW raiser, reached deep in the pipeline below
    every guard written for the file's existing refusals -- measured, six of
    these routes answered 500 while the same KB with a plain duplicate-alias
    file left all of them at 200.

    Moving the refusal to `store_typed_relations` is what fixes it: the error
    now comes from the same place the duplicate-alias refusal always did, so it
    reaches guards that already exist and no new ones were added. Deleting the
    `_refuse_canonical_collisions` call there and refusing in the resolver again
    reddens FOUR of these six -- measured by building it, not four of six
    because six sounded wrong.

    The other two, `/report` and `/questions`, stay 200 even under the bad
    version, because #590 gave them pre-flights that read the alias file and
    degrade before the resolver is ever reached. They are parametrized here
    anyway rather than dropped: they cost nothing, and a change that regressed
    them would be a #590 regression this is the cheapest place to notice. Do
    not read their passing as evidence about THIS guard -- the four are what
    carry it.
    """
    client = _client(tmp_path, _decl(ALIASED) + _decl(SIBLING, "incorporated"))
    r = client.get(route)
    assert r.status_code == 200, f"{route} should degrade, not 500"


def test_a_declaration_under_the_canonical_label_still_wins_alone(tmp_path):
    """The collision refusal must not fire on a KB with one declaration per
    canonical relation, however many relations are declared."""
    store = _store(tmp_path, _decl(ALIASED) + _decl(UNALIASED, "capital"))
    workbench = trust_workbench(store)
    assert len(workbench.corroborated) == 1
