# SPDX-License-Identifier: MPL-2.0
from verinote.pipeline.corroboration import (
    corroboration,
    functional_relations,
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
