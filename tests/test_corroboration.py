# SPDX-License-Identifier: MPL-2.0
import unicodedata

from verinote.pipeline.corroboration import (
    CorroborationPolicyError,
    corroboration,
    functional_relations,
    relation_aliases,
    single_valued_conflicts,
    store_corroboration,
    store_single_valued_conflicts,
)
from verinote.store import Store


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    return s


def test_functional_relations_parse_policy_declarations():
    policy = r'''
.decl functional(rel: symbol)
functional("established_on").
functional("quoted \" relation").
'''

    assert functional_relations(policy) == {
        "established_on",
        'quoted " relation',
    }


def test_corroboration_counts_distinct_engine_sources_only():
    rows = [
        {
            "subject": "Acme",
            "relation": "uses",
            "object": "FastAPI",
            "status": "confirmed",
            "source": "sources/a.md",
        },
        {
            "subject": "Acme",
            "relation": "uses",
            "object": "FastAPI",
            "status": "accepted",
            "source": "sources/b.md",
        },
        {
            "subject": "Acme",
            "relation": "uses",
            "object": "FastAPI",
            "status": "confirmed",
            "source": "sources/b.md",
        },
        {
            "subject": "Acme",
            "relation": "uses",
            "object": "FastAPI",
            "status": "candidate",
            "source": "sources/c.md",
        },
        {
            "subject": "Acme",
            "relation": "uses",
            "object": "FastAPI",
            "status": "confirmed",
            "source": "",
        },
    ]

    support = corroboration(rows)

    assert len(support) == 1
    assert support[0].source_count == 2
    assert support[0].sources == ("sources/a.md", "sources/b.md")


def test_store_corroboration_uses_joined_source_paths(tmp_path):
    s = _store(tmp_path)
    a = s.add_source("sources/a.md")
    b = s.add_source("sources/b.md")
    s.add_fact("Acme", "uses", "FastAPI", status="confirmed", source_id=a)
    s.add_fact("Acme", "uses", "FastAPI", status="confirmed", source_id=b)
    s.add_fact("Acme", "uses", "FastAPI", status="candidate", source_id=b)

    support = store_corroboration(s)

    assert [(x.subject, x.relation, x.object, x.source_count) for x in support] == [
        ("Acme", "uses", "FastAPI", 2)
    ]


def test_single_valued_conflicts_include_per_value_source_support():
    rows = [
        {
            "subject": "Org",
            "relation": "established_on",
            "object": "2020",
            "status": "confirmed",
            "source": "sources/a.md",
        },
        {
            "subject": "Org",
            "relation": "established_on",
            "object": "2021",
            "status": "accepted",
            "source": "sources/b.md",
        },
        {
            "subject": "Org",
            "relation": "established_on",
            "object": "2022",
            "status": "candidate",
            "source": "sources/c.md",
        },
        {
            "subject": "Org",
            "relation": "alias",
            "object": "Acme",
            "status": "confirmed",
            "source": "sources/d.md",
        },
    ]

    conflicts = single_valued_conflicts(rows, {"established_on"})

    assert len(conflicts) == 1
    assert conflicts[0].subject == "Org"
    assert [(v.object, v.source_count) for v in conflicts[0].values] == [
        ("2020", 1),
        ("2021", 1),
    ]


def test_store_single_valued_conflicts_use_default_policy(tmp_path):
    s = _store(tmp_path)
    a = s.add_source("sources/a.md")
    b = s.add_source("sources/b.md")
    s.add_fact("Org", "established_on", "2020", status="confirmed", source_id=a)
    s.add_fact("Org", "established_on", "2021", status="confirmed", source_id=b)

    conflicts = store_single_valued_conflicts(s)

    assert [(c.subject, c.relation) for c in conflicts] == [("Org", "established_on")]


def test_relation_aliases_collapse_surface_variants_for_conflicts():
    rows = [
        {
            "subject": "논문A",
            "relation": "게재연도",
            "object": "2005",
            "status": "confirmed",
            "source": "sources/a.md",
        },
        {
            "subject": "논문A",
            "relation": "발행년도",
            "object": "2007",
            "status": "confirmed",
            "source": "sources/b.md",
        },
    ]
    aliases = {
        "게재연도": "published_year",
        "발행년도": "published_year",
    }

    conflicts = single_valued_conflicts(rows, {"published_year"}, aliases)

    assert [(c.subject, c.relation) for c in conflicts] == [
        ("논문A", "published_year")
    ]
    assert [value.object for value in conflicts[0].values] == ["2005", "2007"]


def test_relation_aliases_same_value_across_variants_is_not_conflict():
    rows = [
        {
            "subject": "논문A",
            "relation": "게재연도",
            "object": "2005",
            "status": "confirmed",
            "source": "sources/a.md",
        },
        {
            "subject": "논문A",
            "relation": "발행년도",
            "object": "2005",
            "status": "confirmed",
            "source": "sources/b.md",
        },
    ]

    conflicts = single_valued_conflicts(
        rows,
        {"published_year"},
        {"게재연도": "published_year", "발행년도": "published_year"},
    )

    assert conflicts == []


def test_relation_aliases_are_opt_in_for_cross_variant_conflicts():
    rows = [
        {
            "subject": "논문A",
            "relation": "게재연도",
            "object": "2005",
            "status": "confirmed",
            "source": "sources/a.md",
        },
        {
            "subject": "논문A",
            "relation": "발행년도",
            "object": "2007",
            "status": "confirmed",
            "source": "sources/b.md",
        },
    ]

    assert single_valued_conflicts(rows, {"published_year"}) == []


def test_relation_aliases_normalize_nfd_surface_names():
    nfd_relation = unicodedata.normalize("NFD", "게재연도")
    rows = [
        {
            "subject": "논문A",
            "relation": nfd_relation,
            "object": "2005",
            "status": "confirmed",
            "source": "sources/a.md",
        },
        {
            "subject": "논문A",
            "relation": "발행년도",
            "object": "2007",
            "status": "confirmed",
            "source": "sources/b.md",
        },
    ]

    conflicts = single_valued_conflicts(
        rows,
        {"published_year"},
        {"게재연도": "published_year", "발행년도": "published_year"},
    )

    assert [(c.subject, c.relation) for c in conflicts] == [
        ("논문A", "published_year")
    ]


def test_relation_aliases_parser_rejects_self_map():
    try:
        relation_aliases("- `published_year` -> `published_year`\n")
    except CorroborationPolicyError as exc:
        assert "self-map" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected CorroborationPolicyError")


def test_store_single_valued_conflicts_loads_relation_alias_file(tmp_path):
    s = _store(tmp_path)
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "logic-policy.dl").write_text(
        '.decl functional(rel: symbol)\nfunctional("published_year").\n',
        encoding="utf-8",
    )
    (policy / "relation-aliases.md").write_text(
        "- `게재연도` -> `published_year`\n"
        "- `발행년도` -> `published_year`\n",
        encoding="utf-8",
    )
    a = s.add_source("sources/a.md")
    b = s.add_source("sources/b.md")
    s.add_fact("논문A", "게재연도", "2005", status="confirmed", source_id=a)
    s.add_fact("논문A", "발행년도", "2007", status="confirmed", source_id=b)

    store_conflicts = store_single_valued_conflicts(s)

    assert [(c.subject, c.relation) for c in store_conflicts] == [
        ("논문A", "published_year")
    ]
