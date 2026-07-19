# SPDX-License-Identifier: MPL-2.0
import builtins

import pytest

import verinote.engine.duckdb_backend as duckdb_backend
from verinote.engine.duckdb_backend import DuckDBInferenceCache, run_check_duckdb
from verinote.engine.duckdb_terms import term_to_duckdb_value
from verinote.engine.terms import Atom, Compound, NumberLit, StringLit


def _duckdb():
    return pytest.importorskip("duckdb")


def test_duckdb_backend_missing_duckdb_is_blocking(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "duckdb":
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    rep = run_check_duckdb([])

    assert rep.ok is False
    assert rep.engine_available is False
    assert rep.errors == 1
    assert "DuckDB is not installed" in rep.text


def test_duckdb_backend_default_policy_flags_functional_conflict():
    _duckdb()
    rep = run_check_duckdb(
        [
            {"subject": "Org", "relation": "established_on", "object": "2020"},
            {"subject": "Org", "relation": "established_on", "object": "2021"},
            {"subject": "Org", "relation": "is_a", "object": "company"},
        ]
    )

    assert rep.engine_available is True
    assert rep.ok is False
    assert rep.errors == 1
    assert rep.warnings == 0
    assert rep.findings == ["ERROR functional_conflict: Org established_on"]
    assert "backend: DuckDB" in rep.text
    assert "--- policy input ---" in rep.text
    assert "--- fact input ---" in rep.text


def test_duckdb_backend_compares_equivalent_atom_string_and_number_terms():
    _duckdb()
    rep = run_check_duckdb(
        [
            {
                "subject": "ada",
                "relation": "born_on",
                "object": "1815",
            },
            {
                "subject": Atom("ada"),
                "relation": Atom("born_on"),
                "object": NumberLit(1900),
            },
        ]
    )

    assert rep.ok is False
    assert rep.findings == ["ERROR functional_conflict: ada born_on"]


def test_duckdb_backend_treats_equal_number_and_string_values_as_equal():
    _duckdb()
    rep = run_check_duckdb(
        [
            {"subject": "ada", "relation": "born_on", "object": "1815"},
            {
                "subject": Atom("ada"),
                "relation": Atom("born_on"),
                "object": NumberLit(1815),
            },
        ]
    )

    assert rep.ok is True
    assert rep.errors == 0


def test_duckdb_backend_keeps_compounds_distinct_from_same_display_string():
    _duckdb()
    compound = Compound("role", (Compound("person", (StringLit("Ada"),)), StringLit("PI")))
    policy = (
        ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
        ".decl answer_q1(value: symbol)\n"
        'answer_q1(S) :- relation(S, "has_role", role(person("Ada"), "PI")).\n'
    )
    rep = run_check_duckdb(
        [
            {
                "subject": "structured",
                "relation": "has_role",
                "object": compound,
            },
            {
                "subject": "display",
                "relation": "has_role",
                "object": 'role(person("Ada"), "PI")',
            },
            {
                "subject": "json",
                "relation": "has_role",
                "object": term_to_duckdb_value(compound),
            },
        ],
        policy_dl=policy,
    )

    assert rep.ok is True
    assert rep.answers == ["q1: structured"]


def test_duckdb_backend_consistent_kb_is_ok():
    _duckdb()
    rep = run_check_duckdb(
        [
            {"subject": "Org", "relation": "established_on", "object": "2020"},
            {"subject": "Org", "relation": "is_a", "object": "company"},
        ]
    )

    assert rep.ok is True
    assert rep.errors == 0
    assert rep.warnings == 0
    assert rep.findings == []
    assert "no findings" in rep.text


def test_duckdb_backend_warn_is_non_blocking():
    _duckdb()
    policy = (
        ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
        ".decl warn_has_isa(subject: symbol)\n"
        'warn_has_isa(S) :- relation(S, "is_a", O).\n'
    )
    rep = run_check_duckdb(
        [{"subject": "Ada", "relation": "is_a", "object": "person"}],
        policy_dl=policy,
    )

    assert rep.ok is True
    assert rep.errors == 0
    assert rep.warnings == 1
    assert rep.findings == ["WARN has_isa: Ada"]


def test_duckdb_backend_answer_query_format_matches_check_report():
    _duckdb()
    query = '.decl answer_q1(value: symbol)\nanswer_q1(O) :- relation("Ada", "born_in", O).\n'
    rep = run_check_duckdb(
        [
            {"subject": "Ada", "relation": "born_in", "object": "London"},
            {"subject": "Ada", "relation": "is_a", "object": "mathematician"},
        ],
        query_dl=query,
    )

    assert rep.ok is True
    assert rep.answers == ["q1: London"]
    assert "q1: London" in rep.text


def test_duckdb_backend_omits_answer_bucket_when_query_has_no_rows():
    _duckdb()
    query = '.decl answer_q1(value: symbol)\nanswer_q1(O) :- relation("Other", "born_in", O).\n'
    rep = run_check_duckdb(
        [{"subject": "Ada", "relation": "born_in", "object": "London"}],
        query_dl=query,
    )

    assert rep.ok is True
    assert rep.answers == []
    assert "--- answers ---" not in rep.text


def test_duckdb_backend_supports_joins_projection_and_duplicates_are_set_semantics():
    _duckdb()
    policy = (
        ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
        ".decl warn_grandparent(subject: symbol, object: symbol)\n"
        'warn_grandparent(A, C) :- relation(A, "parent", B), relation(B, "parent", C).\n'
    )
    rep = run_check_duckdb(
        [
            {"subject": "Ada", "relation": "parent", "object": "Bea"},
            {"subject": "Bea", "relation": "parent", "object": "Cal"},
            {"subject": "Bea", "relation": "parent", "object": "Cal"},
        ],
        policy_dl=policy,
    )

    assert rep.ok is True
    assert rep.warnings == 1
    assert rep.findings == ["WARN grandparent: Ada Cal"]


def test_duckdb_backend_supports_repeated_variables_and_inequality():
    _duckdb()
    policy = (
        ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
        ".decl warn_same(subject: symbol)\n"
        ".decl error_diff(subject: symbol, object: symbol)\n"
        'warn_same(X) :- relation(X, "same_as", X).\n'
        'error_diff(A, B) :- relation(A, "related_to", B), A != B.\n'
    )
    rep = run_check_duckdb(
        [
            {"subject": "Ada", "relation": "same_as", "object": "Ada"},
            {"subject": "Ada", "relation": "related_to", "object": "Bea"},
            {"subject": "Cal", "relation": "related_to", "object": "Cal"},
        ],
        policy_dl=policy,
    )

    assert rep.ok is False
    assert rep.findings == [
        "ERROR diff: Ada Bea",
        "WARN same: Ada",
    ]


def test_duckdb_backend_supports_constants_in_all_atom_positions():
    _duckdb()
    policy = (
        ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
        ".decl warn_exact(subject: symbol)\n"
        'warn_exact("hit") :- relation("Ada", "born_in", "London").\n'
    )
    rep = run_check_duckdb(
        [{"subject": "Ada", "relation": "born_in", "object": "London"}],
        policy_dl=policy,
    )

    assert rep.ok is True
    assert rep.findings == ["WARN exact: hit"]


def test_duckdb_backend_supports_zero_arity_predicates():
    _duckdb()
    policy = (
        ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
        ".decl ready()\n"
        ".decl warn_ready()\n"
        "ready().\n"
        "warn_ready() :- ready().\n"
    )
    rep = run_check_duckdb([], policy_dl=policy)

    assert rep.ok is True
    assert rep.warnings == 1
    assert rep.findings == ["WARN ready: "]


def test_duckdb_backend_supports_ground_compound_and_nested_terms():
    _duckdb()
    policy = (
        ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
        ".decl source(value: symbol)\n"
        '.decl answer_q1(value: symbol)\n'
        'source(role(person("Ada"), "PI")).\n'
        'answer_q1(X) :- source(X), X != role(person("Ada"), "CoPI").\n'
    )
    rep = run_check_duckdb([], policy_dl=policy)

    assert rep.ok is True
    assert rep.answers == ['q1: role(person("Ada"), "PI")']


def test_duckdb_backend_supports_compound_terms_in_base_relation():
    _duckdb()
    query = (
        ".decl answer_q1(value: symbol)\n"
        'answer_q1(S) :- relation(S, "has_role", role(person("Ada"), "PI")).\n'
    )
    rep = run_check_duckdb(
        [
            {
                "subject": Compound("person", (StringLit("Ada"),)),
                "relation": "has_role",
                "object": Compound(
                    "role", (Compound("person", (StringLit("Ada"),)), StringLit("PI"))
                ),
            }
        ],
        query_dl=query,
    )

    assert rep.ok is True
    assert rep.answers == ['q1: person("Ada")']


def test_duckdb_backend_supports_equality_comparison():
    _duckdb()
    policy = (
        ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
        ".decl warn_self(value: symbol)\n"
        'warn_self(X) :- relation(X, "same_as", Y), X == Y.\n'
    )
    rep = run_check_duckdb(
        [
            {"subject": "Ada", "relation": "same_as", "object": "Ada"},
            {"subject": "Bea", "relation": "same_as", "object": "Cal"},
        ],
        policy_dl=policy,
    )

    assert rep.ok is True
    assert rep.findings == ["WARN self: Ada"]


def test_duckdb_backend_rejects_variable_bearing_compound_terms():
    _duckdb()
    policy = (
        ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
        ".decl answer_q1(value: symbol)\n"
        'answer_q1(person(O)) :- relation("Ada", "born_in", O).\n'
    )
    rep = run_check_duckdb(
        [{"subject": "Ada", "relation": "born_in", "object": "London"}],
        policy_dl=policy,
    )

    assert rep.ok is False
    assert "variable-bearing compound" in rep.text


def test_duckdb_backend_invalid_policy_returns_blocking_error():
    _duckdb()
    rep = run_check_duckdb([], policy_dl="this is not datalog")

    assert rep.ok is False
    assert rep.errors == 1
    assert any("engine error" in finding for finding in rep.findings)


@pytest.mark.parametrize(
    "policy",
    [
        (
            ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
            ".decl loop(value: symbol)\n"
            "loop(X) :- loop(X).\n"
        ),
        (
            ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
            ".decl a(value: symbol)\n"
            ".decl b(value: symbol)\n"
            "a(X) :- b(X).\n"
            "b(X) :- a(X).\n"
        ),
    ],
)
def test_duckdb_backend_rejects_recursive_rules(policy):
    _duckdb()
    rep = run_check_duckdb([], policy_dl=policy)

    assert rep.ok is False
    assert "recursive rules are not supported" in rep.text


def test_duckdb_backend_rejects_reserved_comparison_column_collisions():
    _duckdb()
    for column in ("__cmp_value", "__CMP_value"):
        policy = (
            ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
            f".decl warn_collision(value: symbol, {column}: symbol)\n"
            'warn_collision(V, C) :- relation(V, "r", C).\n'
        )

        rep = run_check_duckdb(
            [{"subject": "ada", "relation": "r", "object": "x"}],
            policy_dl=policy,
        )

        assert rep.ok is False
        assert "reserved comparison columns" in rep.text


def test_duckdb_backend_string_values_are_parameterized():
    _duckdb()
    policy = (
        ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
        ".decl warn_injection(value: symbol)\n"
        'warn_injection(O) :- relation("Ada", "says", O).\n'
    )
    payload = 'x"); DROP TABLE "relation"; --'
    rep = run_check_duckdb(
        [{"subject": "Ada", "relation": "says", "object": payload}],
        policy_dl=policy,
    )

    assert rep.ok is True
    assert rep.findings == [f"WARN injection: {payload}"]


def test_duckdb_backend_check_report_fields_are_compatible():
    _duckdb()
    rep = run_check_duckdb([])

    assert isinstance(rep.ok, bool)
    assert isinstance(rep.errors, int)
    assert isinstance(rep.warnings, int)
    assert isinstance(rep.text, str)
    assert isinstance(rep.findings, list)
    assert isinstance(rep.answers, list)
    assert rep.engine_available is True
    assert "facts: 0" in rep.text
    assert "backend: DuckDB" in rep.text
    assert "--- policy input ---" in rep.text
    assert "--- fact input ---" in rep.text


def test_duckdb_inference_cache_reuses_unchanged_relation(monkeypatch):
    _duckdb()
    loads = []
    real_load = duckdb_backend._load_relation_facts

    def counted_load(con, facts):
        loads.append(list(facts))
        return real_load(con, facts)

    monkeypatch.setattr(duckdb_backend, "_load_relation_facts", counted_load)
    cache = DuckDBInferenceCache()
    try:
        facts = [{"subject": "Org", "relation": "is_a", "object": "company"}]
        assert cache.run_check(facts).ok is True
        assert cache.run_check(list(facts)).ok is True
    finally:
        cache.close()

    assert len(loads) == 1


def test_duckdb_inference_cache_reloads_changed_relation(monkeypatch):
    _duckdb()
    loads = []
    real_load = duckdb_backend._load_relation_facts

    def counted_load(con, facts):
        loads.append(list(facts))
        return real_load(con, facts)

    monkeypatch.setattr(duckdb_backend, "_load_relation_facts", counted_load)
    cache = DuckDBInferenceCache()
    try:
        assert cache.run_check(
            [{"subject": "Org", "relation": "established_on", "object": "2020"}]
        ).ok is True
        assert cache.run_check(
            [
                {"subject": "Org", "relation": "established_on", "object": "2020"},
                {"subject": "Org", "relation": "established_on", "object": "2021"},
            ]
        ).ok is False
    finally:
        cache.close()

    assert len(loads) == 2


def test_duckdb_inference_cache_does_not_leak_query_answers():
    _duckdb()
    cache = DuckDBInferenceCache()
    try:
        facts = [{"subject": "Ada", "relation": "born_in", "object": "London"}]
        query = '.decl answer_q1(value: symbol)\nanswer_q1(O) :- relation("Ada", "born_in", O).\n'

        first = cache.run_check(facts, query_dl=query)
        second = cache.run_check(facts)
    finally:
        cache.close()

    assert first.answers == ["q1: London"]
    assert second.answers == []
    assert "--- answers ---" not in second.text


def test_duckdb_inference_cache_does_not_leak_policy_findings():
    _duckdb()
    warning_policy = (
        ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
        ".decl warn_has_isa(subject: symbol)\n"
        'warn_has_isa(S) :- relation(S, "is_a", O).\n'
    )
    quiet_policy = ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
    cache = DuckDBInferenceCache()
    try:
        facts = [{"subject": "Ada", "relation": "is_a", "object": "person"}]
        first = cache.run_check(facts, policy_dl=warning_policy)
        second = cache.run_check(facts, policy_dl=quiet_policy)
    finally:
        cache.close()

    assert first.findings == ["WARN has_isa: Ada"]
    assert second.findings == []


def test_duckdb_backend_rejects_policy_relation_facts():
    _duckdb()
    policy = (
        ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
        'relation("Ghost", "is_a", "phantom").\n'
    )

    rep = run_check_duckdb([], policy_dl=policy)

    assert rep.ok is False
    assert "relation facts must come from SQLite engine input" in rep.text


_NOTE_POLICY = (
    ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
    ".decl error_note(subject: symbol, note: symbol)\n"
    'error_note(S, N) :- relation(S, "note", N).\n'
)
_ANSWER_QUERY = (
    ".decl answer_q1(value: symbol)\n"
    'answer_q1(V) :- relation("Widget", "located_in", V).\n'
)


def _report_body(rep) -> str:
    """The finding body, without the debug echo of the policy/query/fact input."""
    return rep.text.split("\n\n--- policy input ---")[0]


# `backend: DuckDB`, the counts summary, and the blank line before the findings.
_BODY_HEADER_LINES = 3


# Every character `str.splitlines()` treats as a line break, plus NUL and ESC.
# A report line can only be forged by a character that starts a new line, so this
# is the whole attack surface -- not just LF. Guarding LF alone (the old escape
# blacklist) leaves the other eight wide open.
_LINE_BREAKING_CHARS = [
    "\n",  # LF
    "\r",  # CR
    "\x0b",  # VT
    "\x0c",  # FF
    "\x1c",  # FS
    "\x1d",  # GS
    "\x1e",  # RS
    "\x85",  # NEL
    " ",  # LINE SEPARATOR
    " ",  # PARAGRAPH SEPARATOR
]
_NON_PRINTING_CHARS = _LINE_BREAKING_CHARS + ["\x00", "\x1b"]  # NUL, ESC


@pytest.mark.parametrize(
    "char", _NON_PRINTING_CHARS, ids=[f"U+{ord(c):04X}" for c in _NON_PRINTING_CHARS]
)
def test_duckdb_backend_fact_values_cannot_forge_report_lines(char):
    """No fact value can add a line to the report body, whatever it contains.

    This is a structural invariant, not a spot check on `\\n`: the report body
    must hold exactly one line per finding, so the number of `ERROR `/`WARN `
    lines can never exceed what the engine actually derived.
    """
    _duckdb()
    rep = run_check_duckdb(
        [
            {
                "subject": "Widget",
                "relation": "note",
                "object": f"broken{char}ERROR forged: Gadget is unusable",
            },
            {"subject": "Gizmo", "relation": "note", "object": "missing"},
        ],
        policy_dl=_NOTE_POLICY,
    )

    body = _report_body(rep)
    claimed = [
        line
        for line in body.splitlines()
        if line.startswith("ERROR ") or line.startswith("WARN ")
    ]
    assert rep.errors == 2
    assert len(claimed) == rep.errors + rep.warnings
    assert len(rep.findings) == 2
    # The value cannot smuggle in a line at all: one line per finding, exactly.
    assert len(body.splitlines()) == len(rep.findings) + _BODY_HEADER_LINES
    # ...and the forged finding never surfaces as a line of its own, anywhere.
    assert not any(
        line.startswith("ERROR forged") for line in rep.text.splitlines()
    )


def test_duckdb_backend_escapes_control_characters_in_answers():
    _duckdb()
    rep = run_check_duckdb(
        [
            {
                "subject": "Widget",
                "relation": "located_in",
                "object": "alpha\nbeta\tgamma\rdelta",
            }
        ],
        query_dl=_ANSWER_QUERY,
    )

    answer = rep.answers[0]
    assert "\n" not in answer
    assert "\t" not in answer
    assert "\r" not in answer
    assert answer == "q1: alpha\\nbeta\\tgamma\\rdelta"


def test_duckdb_backend_keeps_meaningful_joiners_in_answers():
    """An answer must spell the source document's text, not a mangled copy.

    ZWJ/ZWNJ are required spelling in Persian and Indic scripts and hold emoji
    sequences together. They start no line, so escaping them would only corrupt
    the answer a user reads back. Synthetic values only.
    """
    _duckdb()
    value = "Ba\u200cnu \U0001f468\u200d\U0001f469\u200d\U0001f467"
    rep = run_check_duckdb(
        [{"subject": "Widget", "relation": "located_in", "object": value}],
        query_dl=_ANSWER_QUERY,
    )

    assert rep.answers == [f"q1: {value}"]


def test_duckdb_backend_neutralizes_bidi_overrides_in_answers():
    """RLO forges no newline, but it reverses what the reader sees on the line."""
    _duckdb()
    rep = run_check_duckdb(
        [
            {
                "subject": "Widget",
                "relation": "located_in",
                "object": "safe\u202edangerous",
            }
        ],
        query_dl=_ANSWER_QUERY,
    )

    answer = rep.answers[0]
    assert "\u202e" not in answer
    assert answer == "q1: safe\\u202edangerous"


def test_duckdb_backend_escaping_is_lossless():
    _duckdb()
    rep = run_check_duckdb(
        [{"subject": "Widget", "relation": "located_in", "object": "alpha\nbeta"}],
        query_dl=_ANSWER_QUERY,
    )

    assert "alpha" in rep.answers[0]
    assert "beta" in rep.answers[0]


def test_duckdb_backend_keeps_literal_backslash_distinct_from_newline():
    _duckdb()
    literal = run_check_duckdb(
        [{"subject": "Widget", "relation": "located_in", "object": "alpha\\nbeta"}],
        query_dl=_ANSWER_QUERY,
    )
    newline = run_check_duckdb(
        [{"subject": "Widget", "relation": "located_in", "object": "alpha\nbeta"}],
        query_dl=_ANSWER_QUERY,
    )

    assert "\n" not in newline.answers[0]
    assert literal.answers[0] == "q1: alpha\\\\nbeta"
    assert newline.answers[0] == "q1: alpha\\nbeta"
    assert literal.answers[0] != newline.answers[0]


def test_duckdb_backend_keeps_non_ascii_values_intact():
    _duckdb()
    rep = run_check_duckdb(
        [{"subject": "Widget", "relation": "located_in", "object": "제작소 Gizmo"}],
        query_dl=_ANSWER_QUERY,
    )

    assert rep.answers == ["q1: 제작소 Gizmo"]


def test_duckdb_backend_plain_string_answers_stay_unquoted():
    _duckdb()
    rep = run_check_duckdb(
        [{"subject": "Widget", "relation": "located_in", "object": "Gadget Works"}],
        query_dl=_ANSWER_QUERY,
    )

    assert rep.answers == ["q1: Gadget Works"]
    assert '"' not in rep.answers[0]


_DUP_POLICY = (
    ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
    ".decl error_dup(subject: symbol, object: symbol)\n"
    'error_dup(S, O) :- relation(S, "flag", O).\n'
)
_ROLE_QUERY = (
    ".decl answer_q1(value: symbol)\n"
    'answer_q1(O) :- relation("Subj", "role", O).\n'
)


def _c(functor, *args):
    return Compound(functor, tuple(args))


def test_duckdb_backend_counts_distinct_tuples_that_render_alike():
    """Two policy violations that happen to render the same must stay two.

    ``("A B", "flag", "C")`` and ``("A", "flag", "B C")`` are different tuples,
    but the old rendered-string dedupe collapsed them into a single "1 error"
    (issue #167). The count is what the user trusts, so it must reflect tuples.
    """
    _duckdb()
    rep = run_check_duckdb(
        [
            {"subject": "A B", "relation": "flag", "object": "C"},
            {"subject": "A", "relation": "flag", "object": "B C"},
        ],
        policy_dl=_DUP_POLICY,
    )

    assert rep.ok is False
    assert rep.errors == 2
    assert len(rep.findings) == 2


def test_duckdb_backend_counts_distinct_finding_tuples_that_render_alike():
    """Finding rows must not collapse on their rendered text.

    A compound ``f(x)`` and the string ``"f(x)"`` are distinct engine values,
    but both render as ``f(x)`` in a finding. The report count and structured
    finding rows must preserve both tuples; the source-label annotator can treat
    the duplicate text as ambiguous if it needs a one-line-to-one-row map.
    """
    _duckdb()
    rep = run_check_duckdb(
        [
            {
                "subject": "Subj",
                "relation": "flag",
                "object": _c("f", Atom("x")),
            },
            {"subject": "Subj", "relation": "flag", "object": "f(x)"},
        ],
        policy_dl=_DUP_POLICY,
    )

    assert rep.ok is False
    assert rep.errors == 2
    assert rep.findings == ["ERROR dup: Subj f(x)", "ERROR dup: Subj f(x)"]
    assert [row.text for row in rep.finding_rows] == rep.findings
    assert len({row.identity for row in rep.finding_rows}) == 2


def test_duckdb_backend_finding_columns_cannot_forge_each_other():
    """A space in one column may not blur into the column boundary.

    Without escaping, ``("A B", "C")`` and ``("A", "B C")`` both render
    ``A B C`` and become indistinguishable. Multi-column findings must render so
    the two tuples read differently.
    """
    _duckdb()
    rep = run_check_duckdb(
        [
            {"subject": "A B", "relation": "flag", "object": "C"},
            {"subject": "A", "relation": "flag", "object": "B C"},
        ],
        policy_dl=_DUP_POLICY,
    )

    assert sorted(rep.findings) == ["ERROR dup: A B\\ C", "ERROR dup: A\\ B C"]
    assert len(set(rep.findings)) == 2


def test_duckdb_backend_answer_comma_cannot_forge_two_answers():
    """One value containing a comma must not read as two answers.

    ``Analytical Engine, Ltd`` is a single answer; joined answers use ``, `` as
    the separator, so the value's own comma is escaped to stay distinguishable
    from a two-answer list (issue #167).
    """
    _duckdb()
    query = (
        ".decl answer_q1(value: symbol)\n"
        'answer_q1(O) :- relation("Ada", "worked_at", O).\n'
    )
    rep = run_check_duckdb(
        [{"subject": "Ada", "relation": "worked_at", "object": "Analytical Engine, Ltd"}],
        query_dl=query,
    )

    assert rep.answers == ["q1: Analytical Engine\\, Ltd"]
    # It must not read as the two-answer list ``Analytical Engine`` + ``Ltd``.
    assert rep.answers != ["q1: Analytical Engine, Ltd"]


def test_duckdb_backend_answers_keep_distinct_tuples_that_render_alike():
    """Two different answer tuples that used to render alike must both survive.

    A compound ``pair(a, b)`` and the *string* ``"pair(a, b)"`` are different
    answers: one is a structured term, one is a text value that merely looks like
    it. Under the old renderer both printed ``pair(a, b)`` and ``set()`` on that
    text dropped one answer entirely. Deduping on the tuple keeps both, and the
    string's surface comma is escaped so the two are no longer even confusable
    (issue #167).
    """
    _duckdb()
    rep = run_check_duckdb(
        [
            {"subject": "Subj", "relation": "role", "object": _c("pair", Atom("a"), Atom("b"))},
            {"subject": "Subj", "relation": "role", "object": "pair(a, b)"},
        ],
        query_dl=_ROLE_QUERY,
    )

    # Compound comma stays bare; the string's comma is escaped. Two answers, not
    # one. Reverting the fix (set() on rendered text) collapses these to a single
    # "q1: pair(a, b)".
    assert rep.answers == ["q1: pair(a, b), pair(a\\, b)"]


_PAIR_QUERY = (
    ".decl answer_q1(left: symbol, right: symbol)\n"
    'answer_q1(S, O) :- relation(S, "pair", O).\n'
)


def test_duckdb_backend_multi_column_answer_columns_cannot_forge_each_other():
    """A multi-column answer must guard its column boundary like a finding.

    Answers join columns with a bare space too, so without escaping
    ``("A B", "C")`` and ``("A", "B C")`` both read ``A B C`` and become
    indistinguishable inside the ``, ``-joined answer list (issue #167).
    Reverting the multi-column space escape collapses both rows to ``A B C``.
    """
    _duckdb()
    rep = run_check_duckdb(
        [
            {"subject": "A B", "relation": "pair", "object": "C"},
            {"subject": "A", "relation": "pair", "object": "B C"},
        ],
        query_dl=_PAIR_QUERY,
    )

    assert rep.answers == ["q1: A B\\ C, A\\ B C"]
    # The two tuples must not read alike (the pre-fix bug produced 'A B C, A B C').
    assert rep.answers != ["q1: A B C, A B C"]


def test_duckdb_backend_collapses_representation_twins_into_one_finding():
    """Same value, different storage encoding, is one violation -- not two.

    ``Atom("x")`` and ``StringLit("x")`` are stored as different JSON terms, so
    ``SELECT DISTINCT`` keeps both, but the engine treats them as equal
    (``term_compare_key`` maps both to ``s:x``). The report must dedupe on that
    compare-key tuple, so this pair collapses into a single finding. Reverting
    ``_dedupe_rows_by_compare_key`` yields two identical findings instead.
    """
    _duckdb()
    rep = run_check_duckdb(
        [
            {"subject": Atom("x"), "relation": "flag", "object": "y"},
            {"subject": "x", "relation": "flag", "object": "y"},
        ],
        policy_dl=_DUP_POLICY,
    )

    assert rep.ok is False
    assert rep.errors == 1
    assert rep.findings == ["ERROR dup: x y"]
