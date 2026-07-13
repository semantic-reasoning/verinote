# SPDX-License-Identifier: MPL-2.0
import builtins

import pytest

import verinote.engine.duckdb_backend as duckdb_backend
from verinote.engine.duckdb_backend import DuckDBInferenceCache, run_check_duckdb
from verinote.engine.terms import Compound, StringLit


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
