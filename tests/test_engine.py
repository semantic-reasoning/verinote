# SPDX-License-Identifier: MPL-2.0
import builtins

import pytest

import verinote.engine.wirelog as wl
from verinote.engine import DEFAULT_POLICY, compile_dl, run_check, validate_query
from verinote.engine.terms import (
    StringLit,
    TermParseError,
    escape_string_value,
    parse_term,
    render_term,
)

# A subject with two distinct objects for a functional relation is a conflict.
_CONFLICT = compile_dl(
    [
        {"subject": "Org", "relation": "established_on", "object": "2020"},
        {"subject": "Org", "relation": "established_on", "object": "2021"},
        {"subject": "Org", "relation": "is_a", "object": "company"},
    ]
)
_CONSISTENT = compile_dl(
    [
        {"subject": "Org", "relation": "established_on", "object": "2020"},
        {"subject": "Org", "relation": "is_a", "object": "company"},
    ]
)


def _require_pyrewire():
    return pytest.importorskip("pyrewire")


def test_parse_relation_facts_roundtrips_escaping():
    dl = compile_dl([{"subject": 'a"b', "relation": "r", "object": "c"}])
    assert wl._parse_relation_facts(dl) == [('a"b', "r", "c")]


def test_run_check_flags_functional_conflict():
    _require_pyrewire()
    rep = run_check(_CONFLICT)
    assert rep.engine_available is True
    assert rep.errors > 0
    assert rep.ok is False
    # finding is human-readable: names the subject and the conflicting relation
    joined = "\n".join(rep.findings)
    assert "functional_conflict" in joined
    assert "Org" in joined and "established_on" in joined


def test_run_check_consistent_is_ok():
    _require_pyrewire()
    rep = run_check(_CONSISTENT)
    assert rep.errors == 0
    assert rep.ok is True
    # A consistent KB gates clean: no ERROR findings. (Non-blocking dead_rule
    # WARN notes for the default policy's unused functional relations may appear.)
    assert not any(line.startswith("ERROR") for line in rep.findings)


def test_run_check_empty_kb_is_ok():
    _require_pyrewire()
    rep = run_check("")
    assert rep.ok is True and rep.errors == 0


def test_run_check_uses_custom_policy():
    _require_pyrewire()
    # A policy that flags *any* is_a edge as an error.
    policy = (
        ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
        ".decl error_has_isa(subject: symbol, object: symbol)\n"
        'error_has_isa(S, O) :- relation(S, "is_a", O).\n'
    )
    rep = run_check(_CONSISTENT, policy_dl=policy)
    assert rep.errors == 1
    assert "has_isa: Org company" in "\n".join(rep.findings)


def test_run_check_surfaces_policy_error():
    _require_pyrewire()
    rep = run_check(_CONSISTENT, policy_dl="this is not valid datalog !!!")
    assert rep.ok is False
    assert rep.errors == 1
    assert any("engine error" in f for f in rep.findings)


def test_run_check_degrades_without_engine(monkeypatch):
    monkeypatch.setattr(wl, "_load_engine", lambda: None)
    rep = run_check(_CONFLICT)
    assert rep.engine_available is False
    assert rep.ok is True and rep.errors == 0  # cannot gate without the engine
    assert "not installed" in rep.text


def test_default_policy_declares_finding_relations():
    assert "error_functional_conflict" in DEFAULT_POLICY
    assert ".decl relation(" in DEFAULT_POLICY


def test_run_check_evaluates_query():
    _require_pyrewire()
    dl = compile_dl(
        [
            {"subject": "Ada", "relation": "born_in", "object": "London"},
            {"subject": "Ada", "relation": "is_a", "object": "mathematician"},
        ]
    )
    query = '.decl answer_q1(value: symbol)\nanswer_q1(O) :- relation("Ada", "born_in", O).\n'
    rep = run_check(dl, query_dl=query)
    assert rep.ok is True
    assert rep.answers == ["q1: London"]
    assert "q1: London" in rep.text


def test_run_check_without_query_has_no_answers():
    _require_pyrewire()
    dl = compile_dl([{"subject": "Ada", "relation": "is_a", "object": "x"}])
    assert run_check(dl).answers == []


def test_validate_query_accepts_relation_only():
    ok, reason = validate_query(
        '.decl answer_q1(value: symbol)\nanswer_q1(O) :- relation("a", "b", O).'
    )
    assert ok is True and reason == ""


def test_validate_query_accepts_supported_ground_compound_terms():
    ok, reason = validate_query(
        ".decl answer_q1(value: symbol)\n"
        'answer_q1(S) :- relation(S, "has_role", role(person("Ada"), "PI")).'
    )
    assert ok is True and reason == ""


def test_validate_query_does_not_flag_compound_functors_as_predicates():
    ok, reason = validate_query(
        ".decl answer_q1(value: symbol)\n"
        'answer_q1(person(O)) :- relation(person("Ada"), born_in, O).'
    )
    assert ok is False
    assert "unknown predicate" not in reason
    assert "person" not in reason
    assert "variable-bearing compound" in reason


@pytest.mark.parametrize(
    ("query_dl", "message"),
    [
        (
            ".decl answer_q1(value: symbol)\n"
            'answer_q1(O) :- relation(person(O), "born_in", "London").',
            "variable-bearing compound",
        ),
        (
            ".decl answer_q1(value: symbol)\n"
            'answer_q1(S) :- relation(S, "same_as", O), O == person(S).',
            "variable-bearing compound",
        ),
    ],
)
def test_validate_query_rejects_duckdb_unsupported_compounds(query_dl, message):
    ok, reason = validate_query(query_dl)
    assert ok is False
    assert message in reason


def test_validate_query_does_not_require_pyrewire(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pyrewire":
            raise ImportError("missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    ok, reason = validate_query(
        '.decl answer_q1(value: symbol)\nanswer_q1(O) :- relation("a", "b", O).'
    )
    assert ok is True and reason == ""


def test_validate_query_rejects_unknown_predicate():
    ok, reason = validate_query(".decl answer_q1(value: symbol)\nanswer_q1(O) :- bogus(O).")
    assert ok is False and "bogus" in reason


def test_validate_query_rejects_fabricated_predicate_declarations():
    ok, reason = validate_query(
        ".decl answer_q1(value: symbol)\n"
        ".decl bogus(value: symbol)\n"
        'bogus("x").\n'
        "answer_q1(X) :- bogus(X)."
    )
    assert ok is False and "only declare answer predicates" in reason


@pytest.mark.parametrize(
    "query_dl",
    [
        ".decl answer_q1(value: symbol)\n"
        ".decl answer_query(value: symbol)\n"
        'answer_query(O) :- relation("Ada", "born_in", O).',
        ".decl answer_q1(value: symbol)\n"
        ".decl answer_qevil(a: symbol, b: symbol)\n"
        'answer_qevil(S, O) :- relation(S, "born_in", O).',
    ],
)
def test_validate_query_rejects_fabricated_answer_like_predicates(query_dl):
    ok, reason = validate_query(query_dl)
    assert ok is False
    assert "answer" in reason


def test_validate_query_rejects_arity_mismatch_before_engine():
    ok, reason = validate_query(
        ".decl answer_q1(value: symbol)\n"
        'answer_q1(O) :- relation("Ada", "born_in", O, "extra").'
    )
    assert ok is False and "arity mismatch" in reason


def test_validate_query_rejects_unsafe_variable_before_engine():
    ok, reason = validate_query(
        ".decl answer_q1(value: symbol)\n" 'answer_q1(O) :- relation("Ada", "born_in", X).'
    )
    assert ok is False and "unsafe head variable" in reason


def test_validate_query_rejects_syntax_error():
    ok, reason = validate_query("this is not datalog")
    assert ok is False and reason


# Every character `str.splitlines()` breaks on, plus NUL and ESC. The legacy
# wirelog renderer feeds the same `CheckReport.text`/`.answers` shape as the
# DuckDB backend, so it has to neutralize the same characters -- otherwise the
# next person copies the unescaped path.
_NON_PRINTING_CHARS = [
    "\n", "\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85",
    " ", " ", "\x00", "\x1b",
]


@pytest.mark.parametrize(
    "char", _NON_PRINTING_CHARS, ids=[f"U+{ord(c):04X}" for c in _NON_PRINTING_CHARS]
)
def test_wirelog_row_render_cannot_forge_report_lines(char):
    """A derived tuple's value cannot open a new line in the legacy report body."""
    row = (f"broken{char}ERROR forged: Gadget is unusable", "Widget")

    rendered = wl._render_row(row)

    assert len(rendered.splitlines()) == 1
    assert char not in rendered
    assert not rendered.startswith("ERROR forged")


@pytest.mark.parametrize(
    "char", _NON_PRINTING_CHARS, ids=[f"U+{ord(c):04X}" for c in _NON_PRINTING_CHARS]
)
def test_compile_dl_cannot_forge_report_lines(char):
    """The legacy compiled-input echo is report text too, so it must be escaped."""
    value = f"broken{char}ERROR forged: Gadget is unusable"

    dl = compile_dl([{"subject": "Widget", "relation": "note", "object": value}])

    assert len(dl.splitlines()) == 1
    assert not any(line.startswith("ERROR forged") for line in dl.splitlines())
    assert wl._parse_relation_facts(dl) == [("Widget", "note", value)]


def test_degraded_wirelog_report_cannot_echo_forged_lines(monkeypatch):
    dl = compile_dl(
        [
            {
                "subject": "Widget",
                "relation": "note",
                "object": "broken\nERROR forged: Gadget is unusable",
            }
        ]
    )
    monkeypatch.setattr(wl, "_load_engine", lambda: None)

    rep = run_check(dl)

    assert rep.engine_available is False
    assert not any(line.startswith("ERROR forged") for line in rep.text.splitlines())


def test_wirelog_row_render_keeps_printable_values_intact():
    assert wl._render_row(("Widget", 2020, "Kim Chulsoo")) == "Widget 2020 Kim Chulsoo"


@pytest.mark.parametrize("char", _NON_PRINTING_CHARS, ids=[f"U+{ord(c):04X}" for c in _NON_PRINTING_CHARS])
def test_escaped_string_terms_roundtrip_through_the_parser(char):
    """Escaping stays lossless: what we render, we can read back unchanged."""
    term = StringLit(f"broken{char}ERROR forged")

    rendered = render_term(term)

    assert len(rendered.splitlines()) == 1
    assert parse_term(rendered) == term


@pytest.mark.parametrize(
    "value",
    [
        "London",
        "Kim Chulsoo",
        "김철수",
        "Ada Lovelace",
        "café-naïve",
        "東京",
        "Ωμέγα",
        "emoji 🚀 ok",
    ],
)
def test_printable_values_render_byte_identically(value):
    """Widening the escape set must not touch ordinary text in any language.

    Cc/Zl/Zp contains no letter, mark, digit, punctuation or symbol, so every
    printable string renders exactly as before: quoted, and otherwise verbatim.
    """
    assert render_term(StringLit(value)) == f'"{value}"'
    assert escape_string_value(value) == value


# The escape set is justified by exactly one threat: a value that starts a new
# line forges a report finding. So it has to cover every code point Python
# treats as a line break -- and nothing else may be sacrificed for that.
_SPLITTING_CODE_POINTS = frozenset(
    {0x0A, 0x0B, 0x0C, 0x0D, 0x1C, 0x1D, 0x1E, 0x85, 0x2028, 0x2029}
)


def test_escape_set_covers_every_line_breaking_code_point():
    """Exhaustive proof, not a spot check: scan the whole of Unicode.

    If a future edit narrows the escape set past what `str.splitlines()` breaks
    on, a fact value can forge a report line again. This pins the coverage over
    all 0x110000 code points rather than the handful we happened to think of.
    """
    splitting = {
        cp for cp in range(0x110000) if len(f"a{chr(cp)}b".splitlines()) > 1
    }

    assert splitting == _SPLITTING_CODE_POINTS

    unescaped = sorted(cp for cp in splitting if escape_string_value(chr(cp)) == chr(cp))
    assert unescaped == []
    for cp in splitting:
        assert len(escape_string_value(f"a{chr(cp)}b").splitlines()) == 1


# Written as code points on purpose: these characters are invisible in a source
# file. U+202A..U+202E are the embeddings/overrides, U+2066..U+2069 the isolates.
_BIDI_CONTROLS = [chr(cp) for cp in list(range(0x202A, 0x202F)) + list(range(0x2066, 0x206A))]


@pytest.mark.parametrize(
    "char", _BIDI_CONTROLS, ids=[f"U+{ord(c):04X}" for c in _BIDI_CONTROLS]
)
def test_bidi_controls_are_escaped(char):
    """Bidi overrides break no line, but they reverse what the reader sees.

    A report is a trust artifact: a value must not be able to reorder the text
    of the line it sits on into a claim the engine never derived.
    """
    value = f"safe{char}dangerous"

    escaped = escape_string_value(value)

    assert char not in escaped
    assert escaped == f"safe\\u{ord(char):04x}dangerous"
    assert parse_term(render_term(StringLit(value))) == StringLit(value)


# Zero-width joiners are not decoration: they are required spelling in Persian
# and Indic scripts and they hold emoji sequences together. Escaping them (as
# a blanket Cf rule would) corrupts the source document's spelling, which the
# subject/object rendering promises to preserve. Synthetic samples only.
@pytest.mark.parametrize(
    "value",
    [
        # ZWJ/ZWNJ/soft hyphen spelled as escapes: they are invisible in source.
        pytest.param(
            "\U0001f468\u200d\U0001f469\u200d\U0001f467", id="emoji-family-ZWJ"
        ),
        pytest.param("\U0001f9d1\u200d\U0001f4bb", id="emoji-technologist-ZWJ"),
        pytest.param("می\u200cخواهم", id="persian-ZWNJ"),
        pytest.param("क\u200dष", id="devanagari-ZWJ"),
        pytest.param("क\u200cष", id="devanagari-ZWNJ"),
        pytest.param("co\u00adop", id="soft-hyphen"),
    ],
)
def test_meaningful_format_characters_survive_rendering(value):
    """ZWJ/ZWNJ/soft hyphen render verbatim: they cannot forge a line."""
    assert escape_string_value(value) == value
    assert render_term(StringLit(value)) == f'"{value}"'
    assert len(escape_string_value(value).splitlines()) <= 1
    assert parse_term(render_term(StringLit(value))) == StringLit(value)


# `\u`/`\U` escapes are new in the parser, and the reader is the only thing
# standing between a malformed escape and a lone surrogate reaching UTF-8
# encoding or a DB insert. Callers only catch TermParseError, so every one of
# these has to fail as a parse error -- not as a bare ValueError from `chr()`.
@pytest.mark.parametrize(
    "text",
    [
        pytest.param(r'"\ud800"', id="lone-high-surrogate"),
        pytest.param(r'"\udfff"', id="lone-low-surrogate"),
        pytest.param(r'"\u12"', id="too-few-hex-digits"),
        pytest.param(r'"\u"', id="no-hex-digits"),
        pytest.param(r'"\uzzzz"', id="non-hex-digits"),
        pytest.param(r'"\U00110000"', id="above-max-code-point"),
        pytest.param(r'"\U0011FFFF"', id="far-above-max-code-point"),
    ],
)
def test_parser_rejects_invalid_unicode_escapes(text):
    with pytest.raises(TermParseError):
        parse_term(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(r'"\u0041"', "A", id="bmp-escape"),
        pytest.param(r'"\u0041FF"', "AFF", id="exactly-four-digits-consumed"),
        pytest.param(r'"\U0001f600"', "\U0001f600", id="astral-escape"),
        pytest.param(r'"\U0010FFFF"', "\U0010ffff", id="max-code-point"),
        pytest.param(r'"\u200d"', "\u200d", id="zwj-escape-reads-back"),
    ],
)
def test_parser_accepts_valid_unicode_escapes(text, expected):
    """Rendering escapes only stays lossless if the reader accepts them back."""
    assert parse_term(text) == StringLit(expected)
