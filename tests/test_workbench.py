# SPDX-License-Identifier: MPL-2.0
from verinote.pipeline.workbench import trust_workbench
from verinote.store import Store


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "kb.sqlite")
    s.init_schema()
    return s


def _write_policy(tmp_path) -> None:
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "logic-policy.dl").write_text(
        '.decl functional(rel: symbol)\n'
        'functional("published_year").\n'
        'functional("revenue").\n',
        encoding="utf-8",
    )
    (policy / "relation-aliases.md").write_text(
        "- `pub_year` -> `published_year`\n",
        encoding="utf-8",
    )
    (policy / "typed-relations.md").write_text(
        "- revenue : amount as revenue_scalar\n",
        encoding="utf-8",
    )


def test_workbench_groups_corroboration_with_alias_and_candidates(tmp_path):
    _write_policy(tmp_path)
    s = _store(tmp_path)
    source_a = s.add_source("sources/a.txt")
    source_b = s.add_source("sources/b.txt")
    candidate_source = s.add_source("sources/candidate.txt")
    first = s.add_fact(
        "Sample Report",
        "pub_year",
        "2024",
        status="confirmed",
        source_id=source_a,
    )
    second = s.add_fact(
        "Sample Report",
        "published_year",
        "2024",
        status="accepted",
        source_id=source_b,
    )
    candidate = s.add_fact(
        "Sample Report",
        "published_year",
        "2024",
        status="candidate",
        source_id=candidate_source,
    )

    workbench = trust_workbench(s)

    assert len(workbench.corroborated) == 1
    group = workbench.corroborated[0]
    assert (group.subject, group.relation, group.object) == (
        "Sample Report",
        "published_year",
        "2024",
    )
    assert group.sources == ("sources/a.txt", "sources/b.txt")
    assert [fact.id for fact in group.facts] == [first, second]
    assert group.facts[0].relation_alias == "pub_year -> published_year"
    assert [fact.id for fact in group.related_candidates] == [candidate]


def test_workbench_conflicts_use_typed_scalar_groups_and_separate_candidates(tmp_path):
    _write_policy(tmp_path)
    s = _store(tmp_path)
    source_a = s.add_source("sources/a.txt")
    source_b = s.add_source("sources/b.txt")
    source_c = s.add_source("sources/c.txt")
    candidate_source = s.add_source("sources/candidate.txt")
    s.add_fact(
        "Sample Company",
        "revenue",
        'amount(5000,"억")',
        status="confirmed",
        source_id=source_a,
    )
    s.add_fact(
        "Sample Company",
        "revenue",
        'amount(0.54,"조")',
        status="accepted",
        source_id=source_b,
    )
    s.add_fact(
        "Sample Company",
        "revenue",
        'amount(5400,"억")',
        status="confirmed",
        source_id=source_c,
    )
    candidate = s.add_fact(
        "Sample Company",
        "revenue",
        'amount(7000,"억")',
        status="candidate",
        source_id=candidate_source,
    )

    workbench = trust_workbench(s)

    assert len(workbench.conflicts) == 1
    conflict = workbench.conflicts[0]
    assert (conflict.subject, conflict.relation) == ("Sample Company", "revenue")
    assert [(value.object, value.source_count) for value in conflict.values] == [
        ('amount(0.54,"조")', 2),
        ('amount(5000,"억")', 1),
    ]
    assert [value.typed_normalization for value in conflict.values] == [
        "revenue_scalar=540000000000",
        "revenue_scalar=500000000000",
    ]
    assert [fact.id for fact in conflict.related_candidates] == [candidate]
