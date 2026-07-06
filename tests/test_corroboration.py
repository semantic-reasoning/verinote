# SPDX-License-Identifier: MPL-2.0
import unicodedata

from verinote.pipeline.corroboration import (
    CorroborationPolicyError,
    corroboration,
    functional_relations,
    normalize_typed_value,
    relation_aliases,
    single_valued_conflicts,
    store_corroboration,
    store_relation_aliases,
    store_single_valued_conflicts,
    typed_relations,
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


def test_store_relation_aliases_include_default_policy(tmp_path):
    s = _store(tmp_path)

    aliases = store_relation_aliases(s)

    assert aliases["제공 요소"] == "provides"
    assert aliases["역할"] == "role"


def test_store_relation_aliases_merge_user_policy_with_defaults(tmp_path):
    s = _store(tmp_path)
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "relation-aliases.md").write_text("- `title` -> `role`\n", encoding="utf-8")

    aliases = store_relation_aliases(s)

    assert aliases["title"] == "role"
    assert aliases["제공 요소"] == "provides"


def test_store_relation_aliases_omits_default_that_conflicts_with_user_direction(
    tmp_path,
):
    s = _store(tmp_path)
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "relation-aliases.md").write_text("- `role` -> `역할`\n", encoding="utf-8")

    aliases = store_relation_aliases(s)

    assert aliases["role"] == "역할"
    assert "역할" not in aliases


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


def test_relation_aliases_parser_accepts_plain_arrow_lines():
    assert relation_aliases("- role -> 역할\n- title -> 직함\n") == {
        "role": "역할",
        "title": "직함",
    }


def test_relation_aliases_parser_rejects_malformed_non_empty_lines():
    try:
        relation_aliases("- role 역할\n")
    except CorroborationPolicyError as exc:
        assert "expected `raw` -> `canonical`" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected CorroborationPolicyError")


def test_relation_aliases_parser_rejects_malformed_backticks():
    try:
        relation_aliases("- `role -> 역할\n")
    except CorroborationPolicyError as exc:
        assert "malformed backtick alias" in str(exc)
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


def test_normalize_typed_values_to_comparable_scalars():
    assert normalize_typed_value("date", "2024.7.3") == 20240703
    assert normalize_typed_value("date", "date(2024, 7)") == 20240701
    assert normalize_typed_value("number", "1,234.567") == 1234567
    assert normalize_typed_value("number", "number(1.0005)") == 1001
    assert normalize_typed_value("ordinal", "제3호") == 3
    assert normalize_typed_value("ordinal", "4th") == 4
    assert normalize_typed_value("amount", 'amount(5400,"억")') == 540000000000
    assert normalize_typed_value("amount", 'amount(0.54,"조")') == 540000000000


def test_typed_relations_parse_amount_units():
    specs = typed_relations(
        "- `매출` : amount as revenue (억원=100000000, 조원=1000000000000)\n"
    )

    assert specs["매출"].type == "amount"
    assert specs["매출"].alias == "revenue"
    assert specs["매출"].units == {"억원": 100000000, "조원": 1000000000000}


def test_typed_relations_reject_unit_clause_on_non_amount():
    try:
        typed_relations("- `출시일` : date as released_on (일=1)\n")
    except CorroborationPolicyError as exc:
        assert "units are only valid" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected CorroborationPolicyError")


def test_typed_scalar_equal_values_do_not_conflict():
    rows = [
        {
            "subject": "갑사",
            "relation": "매출",
            "object": 'amount(5400,"억")',
            "status": "confirmed",
            "source": "sources/a.md",
        },
        {
            "subject": "갑사",
            "relation": "매출",
            "object": 'amount(0.54,"조")',
            "status": "confirmed",
            "source": "sources/b.md",
        },
    ]
    typed = typed_relations("- `매출` : amount as revenue\n")

    conflicts = single_valued_conflicts(rows, {"매출"}, typed=typed)

    assert conflicts == []


def test_typed_scalar_equal_values_through_alias_do_not_conflict():
    rows = [
        {
            "subject": "갑사",
            "relation": "매출액",
            "object": 'amount(5400,"억")',
            "status": "confirmed",
            "source": "sources/a.md",
        },
        {
            "subject": "갑사",
            "relation": "revenue",
            "object": 'amount(0.54,"조")',
            "status": "confirmed",
            "source": "sources/b.md",
        },
    ]
    aliases = {"매출액": "revenue"}
    typed = typed_relations("- revenue : amount as revenue_scalar\n")

    conflicts = single_valued_conflicts(rows, {"revenue"}, aliases, typed)

    assert conflicts == []


def test_typed_scalar_different_values_conflict_with_representatives():
    rows = [
        {
            "subject": "갑사",
            "relation": "매출",
            "object": 'amount(5000,"억")',
            "status": "confirmed",
            "source": "sources/a.md",
        },
        {
            "subject": "갑사",
            "relation": "매출",
            "object": 'amount(5400,"억")',
            "status": "confirmed",
            "source": "sources/b.md",
        },
        {
            "subject": "갑사",
            "relation": "매출",
            "object": 'amount(0.54,"조")',
            "status": "accepted",
            "source": "sources/c.md",
        },
    ]
    typed = typed_relations("- `매출` : amount as revenue\n")

    conflicts = single_valued_conflicts(rows, {"매출"}, typed=typed)

    assert [(c.subject, c.relation) for c in conflicts] == [("갑사", "매출")]
    assert [value.object for value in conflicts[0].values] == [
        'amount(0.54,"조")',
        'amount(5000,"억")',
    ]
    assert [value.source_count for value in conflicts[0].values] == [2, 1]


def test_typed_scalar_lookup_uses_nfc_relation_without_changing_conflict_key():
    nfd_relation = unicodedata.normalize("NFD", "매출")
    rows = [
        {
            "subject": "갑사",
            "relation": nfd_relation,
            "object": 'amount(5000,"억")',
            "status": "confirmed",
            "source": "sources/a.md",
        },
        {
            "subject": "갑사",
            "relation": nfd_relation,
            "object": 'amount(0.54,"조")',
            "status": "confirmed",
            "source": "sources/b.md",
        },
    ]
    typed = typed_relations("- `매출` : amount as revenue\n")

    conflicts = single_valued_conflicts(rows, {nfd_relation}, typed=typed)

    assert [(c.subject, c.relation) for c in conflicts] == [("갑사", nfd_relation)]
    assert [value.source_count for value in conflicts[0].values] == [1, 1]


def test_store_single_valued_conflicts_loads_typed_relations_file(tmp_path):
    s = _store(tmp_path)
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "logic-policy.dl").write_text(
        '.decl functional(rel: symbol)\nfunctional("매출").\n',
        encoding="utf-8",
    )
    (policy / "typed-relations.md").write_text(
        "- `매출` : amount as revenue\n",
        encoding="utf-8",
    )
    a = s.add_source("sources/a.md")
    b = s.add_source("sources/b.md")
    s.add_fact("갑사", "매출", 'amount(5400,"억")', status="confirmed", source_id=a)
    s.add_fact("갑사", "매출", 'amount(0.54,"조")', status="confirmed", source_id=b)

    assert store_single_valued_conflicts(s) == []
