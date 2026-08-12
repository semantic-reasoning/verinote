# SPDX-License-Identifier: MPL-2.0
import pytest


# --- the measure-unit caveat (#445) ----------------------------------------
#
# `korean_measure_unit_mismatch` decides whether a verified answer is shown with
# a caveat saying it is stated in a different unit from the one the question
# asked in. Every assertion here is on the exact pair or on None: a mutation that
# returned the family key instead of the spelling would print "the verified value
# states MONTH" to a Korean reader, and only an exact comparison catches that.

_PROSE_VALUES = (
    "샘플부서 연간 계획 수립",
    "연내 완료 예정",
    "주간 보고 체계 운영",
    "일정 조율 진행 중",
    "분기별 실적 검토",
    "시간제 근무 허용",
    "초기 검토 단계",
    "원격 근무 원칙",
    "달성 여부 미정",
    "내년 상반기 착수",
    "지원 대상 아님",
    "분야별 담당 지정",
    "개요 작성 완료",
    "일반 관리 대상",
    "주요 위험 식별",
    "초안 검토 요청",
    "연구 개발 과제",
    "세부 항목 없음",
    "배포 준비 완료",
    "유로존 시장 조사",
    "엔진 점검 필요",
    "달력 기준 산정",
    "세금 계산 제외",
    "살균 처리 완료",
    "프로세스 표준화",
    "퍼센트 표기 통일",
    "제3자 검토 필요",
    "2단계 진행 중",
    "3차 회의 예정",
    "제1분기 마감",
    "5개년 계획 수립",
    "1순위 과제",
    "샘플문서 v2 초안",
    "2년차 담당자 배정",
    "3일간의 일정 확정",
    "3주간격 점검",
    "6월 착수 예정",
    "2021년 기준",
    "second review pending",
    "yearly summary drafted",
    "monthly report archived",
    "weekly sync scheduled",
    "daily standup held",
    "hourly rate undisclosed",
    "minute taking assigned",
    "percentage not disclosed",
    "dollar cost averaging",
    "3 secondary reviews",
    "2 daylight sessions",
)
"""Synthetic values a KB could plausibly hold that state no quantity.

Each one contains a syllable or word that *is* a unit spelling -- `연간`,
`주간`, `일정`, `분기`, `시간제`, `초기`, `원격`, `달성`, `내년`, `지원`,
`분야`, `세금`, `살균`, `프로세스`, `second`, `weekly` -- with no number in
front of it. Fourteen of them do carry a digit, as near-misses: an ordinal
(`제3자`), a stage number (`2단계`), a sequence number (`3차`), a quarter
(`제1분기`), a plan title (`5개년`), a rank (`1순위`), a version (`v2`), a unit
run into the next syllable (`2년차`), two suffixes outside the closed set
(`3일간의`, `3주간격`), a bare month (`6월`), a calendar year (`2021년`), and two
Latin words that begin with a unit spelling (`3 secondary`, `2 daylight`).
"""


def _unit_bearing_counters() -> tuple[str, ...]:
    """The counters a question can ask in that also name a unit.

    Derived from the two tables rather than listed, so the sweep below covers
    every counter that can reach the caveat as the table stands.
    """
    from verinote.pipeline.query_intent import _KOREAN_MEASURE_COUNTER
    from verinote.pipeline.query_measure_unit import _MEASUREMENT_UNIT_SPELLINGS

    return tuple(
        counter
        for counter in _KOREAN_MEASURE_COUNTER.split("|")
        if counter in _MEASUREMENT_UNIT_SPELLINGS
    )


def test_a_measure_question_answered_in_another_unit_names_both_spellings():
    """The headline case from #445, at the level that decides it.

    `샘플사업의 기간은 몇 개월인가?` against a KB holding `2년` is answered `2년`
    and verified, and that stays true -- this only supplies the pair the caveat
    beside it is worded from.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 개월인가?", "2년") == (
        "개월",
        "년",
    )


@pytest.mark.parametrize(
    ("question", "value"),
    [
        ("샘플사업의 기간은 몇 개월인가?", "6개월"),
        ("샘플사업의 기간은 몇 개월인가?", "2년 6개월"),
        ("샘플사업의 기간은 몇 달인가?", "6개월"),
        ("샘플사업의 비율은 몇 퍼센트인가?", "30%"),
        ("샘플인물의 나이는 몇 살인가?", "30세"),
    ],
)
def test_the_asked_unit_stated_anywhere_in_the_value_suppresses_the_caveat(question, value):
    """A value that does state the asked unit has nothing to caveat.

    `2년 6개월` answers `몇 개월인가?` in months among other things, so the
    earlier `년` must not fire.

    The last three pin THIS suppression and nothing else. Their asked counter is
    a table row (`달`, `퍼센트`, `살`) whose value-side spelling is a different
    row of the same canonical unit, so deleting the row under test silences the
    *question* side too and the case stays green -- it cannot pin the row. The
    rows are pinned one-sidedly by the test below.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch(question, value) is None


@pytest.mark.parametrize(
    ("question", "value", "expected"),
    [
        ("샘플사업의 기간은 몇 년인가?", "6달", ("년", "달")),
        ("샘플사업의 증가율은 몇 배인가?", "30%", ("배", "%")),
        ("샘플인물의 나이는 몇 살인가?", "24개월", ("살", "개월")),
    ],
)
def test_a_spellings_row_is_pinned_by_a_question_asked_in_a_different_row(
    question, value, expected
):
    """One row of `_MEASUREMENT_UNIT_SPELLINGS` per case, from one side only.

    The table serves both the question and the value, so a case whose asked
    counter is the row under test cannot pin it. Each case here asks in one row
    and is answered in the row being pinned: delete `달` and `6달` states no unit
    at all, delete `%` and `30%` states none, delete `살` and the question names
    no unit. Every one of the three goes silent, and none of them is masked by
    the same-unit suppression.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch(question, value) == expected


@pytest.mark.parametrize(
    ("question", "value"),
    [
        ("샘플사업의 항목은 몇 개인가?", "2년"),
        ("샘플사업의 기간은 얼마나 되나요?", "2년"),
    ],
)
def test_a_tail_naming_no_unit_is_silent(question, value):
    """`몇 개` and `얼마나` reach the same answer by different routes.

    `개` is a counter `_KOREAN_MEASURE_COUNTER` lists and
    `_MEASUREMENT_UNIT_SPELLINGS` does not, so no unit is derived from it and the
    family table is never consulted. The `얼마나` branch captures no counter at
    all, so the lookup is handed None. Both land on the same `.get` returning
    None rather than on a branch apiece.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch(question, value) is None


def test_a_relation_literally_named_with_a_counter_is_not_a_measure_question():
    """`샘플사업의 몇 년인가?` asks *for* `몇 년`, not *in* years.

    The measure tail is the whole label, so nothing is left in front of it to
    read as a relation -- and `_korean_attribute_label_readings` correspondingly
    reads the label whole and asks the KB for a relation named `몇 년`. A KB that
    holds one answers it, and telling that reader the value is in the wrong unit
    would be nonsense.
    """
    from verinote.pipeline.query_intent import _korean_attribute_label_readings
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    assert _korean_attribute_label_readings("몇 년") == ("몇 년",)
    assert korean_measure_unit_mismatch("샘플사업의 몇 년인가?", "24개월") is None


@pytest.mark.parametrize(
    ("question", "value"),
    [
        ("샘플인물의 역할은 무엇인가?", "2년"),
        ("샘플기간의 최근 몇 년인가?", "재검토"),
        ("샘플행사의 참석자는 몇 분인가?", "5"),
        ("샘플사업의 주기는 몇 시간인가?", "일 단위 관리"),
        ("샘플사업의 가격은 몇 원인가?", "지원 없음"),
    ],
)
def test_a_question_or_value_with_nothing_to_compare_is_silent(question, value):
    """Nothing to compare on one side or the other.

    `역할은 무엇인가?` has no measure tail. The other four have one and a value
    that states no quantity.

    `참석자는 몇 분인가?` answered `5` is a labelled contract regression rather
    than a pinned mechanism: the value engages no value-side machinery at all, so
    no mutation of this rule can make it fire, and it is kept to record that a
    bare number is never caveated.

    `지원 없음` is likewise carried by the sweep rather than by itself: the bare
    `원` inside `지원` is the unit the question asked in, so the same-unit
    suppression would silence it even with the digit requirement removed. The
    one-sided case for that requirement is in the test below.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch(question, value) is None


def test_the_digit_requirement_keeps_ordinary_prose_out_of_the_caveat(monkeypatch):
    """Every unit-bearing counter against a corpus of prose values, all silent.

    This is what the leading `[0-9]` in `_VALUE_MEASUREMENT` buys, and the sweep
    rather than any single case is what measures it.

    The mutant count is BUILT AND MEASURED here rather than quoted in prose. It
    was quoted once, as 55, and went stale the moment the suppression scan got
    its own pattern: with one pattern serving both halves, mutating
    `_VALUE_MEASUREMENT` mutated suppression too, and 13 of the fires that
    number counted are now suppressed by `_VALUE_MEASUREMENT_RELAXED` instead.
    A figure in a docstring cannot notice that; this assertion can.

    A one-sided case for the requirement, not masked by the same-unit
    suppression: `몇 개월인가?` answered `내년 착수` is silent as written and
    fires `(개월, 년)` without the digit prefix.
    """
    import re

    import verinote.pipeline.query_measure_unit as qmu
    from verinote.pipeline.query_measure_unit import (
        _VALUE_MEASUREMENT,
        korean_measure_unit_mismatch,
    )

    counters = _unit_bearing_counters()
    assert len(counters) == 13
    questions = [f"샘플대상의 지표는 몇 {counter}인가?" for counter in counters]
    pairs = [(question, value) for question in questions for value in _PROSE_VALUES]
    assert len(pairs) == len(counters) * len(_PROSE_VALUES)

    for question, value in pairs:
        assert korean_measure_unit_mismatch(question, value) is None, (question, value)

    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 개월인가?", "내년 착수") is None

    # The mutation the sentence above names: the reporting pattern only, with
    # the suppression pattern left alone. Mutating both instead gives 55, which
    # is what the stale figure was measuring.
    #
    # Sliced off the live pattern rather than rebuilt from parts, so the mutant
    # is this pattern minus its digit head and nothing else. A hand-built copy
    # would drift from the source silently, and asserting text equality against
    # one would fail whenever the alternation is legitimately reordered -- the
    # `startswith` is the tie, and it is the only thing this hardcodes.
    digit_head = r"[0-9][0-9,.]*"
    assert _VALUE_MEASUREMENT.pattern.startswith(digit_head)
    without_digits = re.compile(_VALUE_MEASUREMENT.pattern[len(digit_head):])
    monkeypatch.setattr(qmu, "_VALUE_MEASUREMENT", without_digits)
    fires = sum(
        1 for question, value in pairs
        if korean_measure_unit_mismatch(question, value) is not None
    )
    assert fires == 68


def test_a_cross_family_unit_is_not_a_unit_mismatch():
    """`몇 원인가?` answered `2년` is not a money value in the wrong currency.

    Money and time are not convertible, so "no unit conversion is applied" would
    be the wrong thing to say about it. Within a family the caveat is right:
    `1000달러` answering `몇 원인가?` is the same quantity in another currency.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch("샘플사업의 가격은 몇 원인가?", "2년") is None
    assert korean_measure_unit_mismatch("샘플사업의 가격은 몇 원인가?", "1000달러") == (
        "원",
        "달러",
    )


@pytest.mark.parametrize(
    ("question", "value", "expected"),
    [
        ("샘플사업의 기간은 몇 개월인가?", "30% 완료, 3주", ("개월", "주")),
        ("샘플사업의 가격은 몇 원인가?", "30% 할인, 3달러", ("원", "달러")),
        ("샘플사업의 기간은 몇 개월인가?", "2배 증가, 3주 소요", ("개월", "주")),
    ],
)
def test_the_first_same_family_unit_is_reported_not_the_first_unit(
    question, value, expected
):
    """An earlier cross-family quantity does not silence a later same-family one.

    Each value states a ratio first and the quantity the question was about
    second. Reporting the first unit found would name a percentage or a multiple
    beside a question about months or won, and reverting the selection to the
    first unit makes all three go silent instead, because the ratio is in the
    wrong family.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch(question, value) == expected


def test_a_dated_value_no_longer_silences_the_units_stated_beside_it():
    """A date is a point in time, and only its own span is silenced.

    Named for dates because that is what its values are; `_TIME_POINT` now
    covers more than dates, and the rule in its general form is
    `test_a_point_in_time_silences_only_its_own_span`.

    `2021년` must not be reported as stating years, and it is not: the whole of
    it sits inside its own span, so nothing is left to report. What #452 changed
    is the other two. The guard is span-local, so a duration standing outside
    every span is still read, and `3주` and `30분` are each a real same-family
    mismatch that this rule now names.

    This is also where a family-level repeat rule dies. Suppressing a leaked
    quantity whose unit some span in the same value already states was measured
    against #452 and rejected; at the coarser grain of the measurement family it
    would silence `2021년 착수, 총 3주` here, since `년` and `주` are both
    durations, so the second assertion is that candidate's killer as well.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 개월인가?", "2021년") is None
    assert korean_measure_unit_mismatch(
        "샘플사업의 기간은 몇 개월인가?", "2021년 착수, 총 3주"
    ) == ("개월", "주")
    assert korean_measure_unit_mismatch("샘플회의의 시간은 몇 시간인가?", "2021년 기준 30분") == (
        "시간",
        "분",
    )


def test_a_longer_digit_run_is_not_read_as_a_calendar_year():
    """The left-hand `(?<![0-9])`: without it, `10000년` matches on `0000년`.

    A genuine ten-thousand-year value would then be read as a calendar date and
    silenced. The right-hand bound is pinned separately below. `1500년` and
    `2000년간` stay on the date side; both spellings are really used for
    durations too, so that reading is ambiguous and this rule picks the date one.
    """
    from verinote.pipeline.query_measure_unit import (
        _value_measure_units,
        korean_measure_unit_mismatch,
    )

    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 개월인가?", "10000년") == (
        "개월",
        "년",
    )
    assert _value_measure_units("1500년") == ()
    assert _value_measure_units("2000년간") == ()


@pytest.mark.parametrize(
    "value",
    ["2년차", "3일간의 일정", "3주간격", "제1분기", "3 secondary reviews"],
)
def test_a_unit_continued_by_another_letter_is_not_read(value):
    """The trailing lookahead: a unit run into Hangul, a digit, or Latin is not one.

    `2년차` is a second year of service, `3일간의 일정` and `3주간격` carry
    suffixes outside the closed set, `제1분기` is a quarter, and
    `3 secondary reviews` begins with `second` and continues in Latin. Each is
    asked in a *different* unit of the same family, so none of them is masked by
    the same-unit suppression -- `몇 초인가?` would have masked the last one.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 개월인가?", value) is None


def test_a_suffix_inside_the_closed_set_still_reads_the_unit():
    """`2년간` states two years; the suffix does not hide the unit."""
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 개월인가?", "2년간") == (
        "개월",
        "년",
    )


def test_a_longer_spelling_is_reached_by_backtracking_past_a_shorter_one():
    """`주일` and `달러` are read even though `주` and `달` are tried first.

    The alternation is in table order, so `주` and `달` match first and are then
    rejected by the lookahead, which sees the Hangul that follows them; the
    engine backtracks into the longer row. That mechanism, not the ordering, is
    what makes these read -- every prefix pair in the table today is extended by
    a Hangul or Latin character, both inside the lookahead's class.
    """
    from verinote.pipeline.query_measure_unit import _value_measure_units

    assert _value_measure_units("2주일") == (("WEEK", "주일"),)
    assert _value_measure_units("3달러") == (("USD", "달러"),)


def test_bare_월_is_a_month_of_the_year_and_is_not_read():
    """`3월` is March and `6월` is June, so neither states a month-count.

    `개월` is the counter for a month-count and is in the table; bare `월` is
    deliberately not. `일` is in the table because `3일` is three days far more
    often than it is the third of the month, and a day of the month arrives
    with a month term in front of it, which `_TIME_POINT` catches.
    """
    from verinote.pipeline.query_measure_unit import (
        _value_measure_units,
        korean_measure_unit_mismatch,
    )

    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 일인가?", "3월 착수") is None
    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 개월인가?", "6월") is None
    assert _value_measure_units("3일") == (("DAY", "일"),)


def test_a_prefix_currency_symbol_is_out_of_reach_by_construction():
    """`$`, `₩` and `€` precede their number, and every quantity starts at a digit.

    Their absence from the table is therefore not a row that could be restored:
    no row spelled that way would ever be reached. `%` is read because it follows
    the number.
    """
    from verinote.pipeline.query_measure_unit import _value_measure_units

    assert _value_measure_units("$1000") == ()
    assert _value_measure_units("₩1000") == ()
    assert _value_measure_units("€1000") == ()
    assert _value_measure_units("30%") == (("PERCENT", "%"),)


@pytest.mark.parametrize(
    ("question", "value", "expected"),
    [
        ("샘플사업의 기간은 몇 개월인가?", "30세", ("개월", "세")),
        ("샘플사업의 증가율은 몇 배인가?", "120퍼센트", ("배", "퍼센트")),
    ],
)
def test_further_rows_report_the_spelling_the_value_used(question, value, expected):
    """The stated spelling is reported, never the canonical unit.

    `30세` states `세`, not `YEAR`, and `120퍼센트` states `퍼센트`, not
    `PERCENT`. A caveat naming the family key would print an English identifier
    to a Korean reader.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch(question, value) == expected


def test_a_value_written_in_nfd_states_its_unit():
    """The value is composed before it is read.

    `Store.add_fact` keeps the `object` column as written, so a decomposed `2년`
    reaches this rule as four code points while the alternation is composed. Drop
    the `nfc` call and this value states nothing.
    """
    import unicodedata

    from verinote.pipeline.query_measure_unit import _value_measure_units

    decomposed = unicodedata.normalize("NFD", "2년")
    assert len(decomposed) == 4
    assert _value_measure_units(decomposed) == (("YEAR", "년"),)


_ROW_FIXTURES = {
    "년": "2년", "연": "3연", "살": "30살", "세": "30세",
    "year": "1 year", "years": "2 years",
    "개월": "6개월", "달": "6달", "month": "1 month", "months": "2 months",
    "주": "3주", "주일": "2주일", "week": "1 week", "weeks": "2 weeks",
    "일": "3일", "day": "1 day", "days": "2 days",
    "시간": "5시간", "hour": "1 hour", "hours": "2 hours",
    "분": "30분", "minute": "1 minute", "minutes": "2 minutes",
    "초": "10초", "second": "1 second", "seconds": "2 seconds",
    "퍼센트": "30퍼센트", "프로": "30프로", "%": "30%", "percent": "30 percent",
    "배": "2배",
    "원": "1000원", "won": "1000 won",
    "달러": "3달러", "dollar": "1 dollar", "dollars": "2 dollars",
    "엔": "1000엔", "yen": "1000 yen",
    "유로": "500유로", "euro": "500 euro",
}
"""One value per row of `_MEASUREMENT_UNIT_SPELLINGS`, stating that row's unit.

`_value_measure_units` reads only the value, so a fixture here is one-sided by
construction: deleting the row it exercises leaves its value stating nothing,
whatever the question. That is what the mismatch-level cases cannot do for a row
whose only counter is the row itself -- `won` has no second money counter to ask
in, since `원` is the only money spelling `_KOREAN_MEASURE_COUNTER` lists.

`3연` is the one contrived value: `연` means a year but is not used as a counter
after a number in ordinary Korean, so this pins the row's reachability rather
than a spelling a KB would hold.
"""


def test_every_spellings_row_has_a_value_that_states_it():
    """Each row read back one-sidedly, and no row left without a fixture.

    The equality on `_ROW_FIXTURES`' key set is the half that keeps this honest:
    a row added to the table with no value exercising it fails here rather than
    joining silently.
    """
    from verinote.pipeline.query_measure_unit import (
        _MEASUREMENT_UNIT_SPELLINGS,
        _value_measure_units,
    )

    assert set(_ROW_FIXTURES) == set(_MEASUREMENT_UNIT_SPELLINGS)
    for spelling, value in _ROW_FIXTURES.items():
        assert _value_measure_units(value) == (
            (_MEASUREMENT_UNIT_SPELLINGS[spelling], spelling),
        ), (spelling, value)


def test_a_korean_magnitude_word_between_the_digits_and_the_unit_is_read():
    """`3만 달러` is thirty thousand dollars, and the `만` must not break the read.

    Without `[만억천조]?` in `_VALUE_MEASUREMENT` the digits no longer reach the
    unit and the value states nothing.
    """
    from verinote.pipeline.query_measure_unit import (
        _value_measure_units,
        korean_measure_unit_mismatch,
    )

    assert _value_measure_units("3만 달러") == (("USD", "달러"),)
    assert korean_measure_unit_mismatch("샘플사업의 가격은 몇 원인가?", "3만 달러") == (
        "원",
        "달러",
    )


def test_a_latin_spelling_is_matched_and_reported_casefolded():
    """The value is casefolded before it is read, and that shows in the caveat.

    `3 Weeks` states weeks, and the sentence beside the answer will say the value
    states `weeks` rather than `Weeks`. Reporting the folded spelling is the
    accepted cost of matching a table written in lower case.
    """
    from verinote.pipeline.query_measure_unit import (
        _value_measure_units,
        korean_measure_unit_mismatch,
    )

    assert _value_measure_units("3 Weeks") == (("WEEK", "weeks"),)
    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 개월인가?", "3 Weeks") == (
        "개월",
        "weeks",
    )


def test_a_four_digit_year_run_into_more_digits_is_not_a_calendar_year():
    """The two bounds on the four-digit year are not symmetric, and only one bites.

    The left-hand `(?<![0-9])` is live: without it `10000년` matches on its inner
    `0000년`, the real `10000년` straddles that span's left edge, and a genuine
    ten-thousand-year duration is dropped. `test_a_longer_digit_run_is_not_read_as_a_calendar_year`
    is where that is pinned.

    The right-hand `(?![0-9])` cannot bite under a span-local guard, and the
    reason is not a corpus result but an entailment. For it to matter a quantity
    would have to overlap the span `NNNN년`; that span ends at `년`, and the bound
    only fires when a digit follows, so any quantity ending there is refused by
    `_VALUE_MEASUREMENT`'s own trailing lookahead, and any quantity starting
    after it begins exactly at the span's end. Nothing can cross that edge.

    It is kept anyway, because the entailment leans on a pattern one screen away
    rather than on this one: widen `_VALUE_MEASUREMENT`'s lookahead to admit a
    unit run into a digit and the bound becomes load-bearing again with nothing
    local holding the line. So the assertions below are the inertness itself and
    the property it rests on -- the second is the tripwire, and it fails the
    moment that reading changes.
    """
    import re

    from verinote.pipeline.query_measure_unit import (
        _TIME_POINT,
        _VALUE_MEASUREMENT,
        _value_measure_units,
        korean_measure_unit_mismatch,
    )

    # The right bound is inert: the same readings with it and without it.
    without = re.compile(_TIME_POINT.pattern.replace(
        r"|(?<![0-9])[0-9]{4}\s*년(?![0-9])", r"|(?<![0-9])[0-9]{4}\s*년"))
    assert without.pattern != _TIME_POINT.pattern, "the mutation did not apply"
    import verinote.pipeline.query_measure_unit as qmu
    for value in ["2021년12개월", "2021년 12개월", "2021년12주", "2021년1,000원", "2021년"]:
        original = qmu._TIME_POINT
        qmu._TIME_POINT = without
        try:
            relaxed = qmu._value_measure_units(value)
        finally:
            qmu._TIME_POINT = original
        assert _value_measure_units(value) == relaxed, value

    # And the property that makes it inert: the year is not read as a quantity
    # at all, because a digit follows it.
    assert [m.group(0) for m in _VALUE_MEASUREMENT.finditer("2021년12개월")] == ["12개월"]
    assert _value_measure_units("2021년12개월") == (("MONTH", "개월"),)
    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 주인가?", "2021년12개월") == (
        "주",
        "개월",
    )


@pytest.mark.parametrize(
    ("question", "value", "branch", "unit_it_would_wrongly_state"),
    [
        ("샘플계약의 기간은 몇 개월인가?", "3월 15일 착수", "day of month", "일"),
        ("샘플회의의 시간은 몇 시간인가?", "3시 30분", "clock hour", "분"),
        ("샘플계약의 기간은 몇 개월인가?", "'21년", "apostrophe year", "년"),
        ("샘플계약의 기간은 몇 개월인가?", "21년 3월 ~ 22년 2월", "year and month", "년"),
        ("샘플계약의 기간은 몇 개월인가?", "2021년 착수", "four-digit year", "년"),
        ("샘플계약의 기간은 몇 개월인가?", "2021.03.15 일", "ISO-style date", "일"),
    ],
)
def test_each_time_point_branch_is_needed_by_one_of_these_values(
    question, value, branch, unit_it_would_wrongly_state
):
    """One value per alternative of `_TIME_POINT`, so no branch rides free.

    Every alternative needs its own killer. Pinning only the four-digit-year
    branch once left the others deletable with the suite green, which is the
    same defect as a spellings row with no fixture: one covered alternative
    makes the whole guard look covered. The two member tuples are covered by
    their own tests, not by this one.

    Deleting the branch each value exercises makes exactly that value fire, and
    only that value -- the deletion matrix is a clean diagonal, so none is
    masking another. The last parameter records what the caveat would then
    wrongly say the value states. The question travels with the value because
    the clock row has to be asked in `몇 시간`; asked that way it is still safe
    from the same-unit suppressor, because `_value_states_asked_unit` searches
    for the spelling `시간` and `3시 30분` does not contain it.

    The ISO value is the one that justifies its branch rather than merely
    exercising it. A bare `2021-03-15` states no unit, so the branch does nothing
    for it; what the branch is for is a unit spelling run onto the end of a date,
    which is read as days without it. That branch used to cost real caveats as
    well -- `2024/01/02 3주` states a genuine duration and went silent with the
    date -- and #452 ended that half of the cost by making the guard span-local,
    so the duration standing clear of the date is reported again.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch(question, value) is None


def test_a_full_width_number_states_no_quantity():
    """`[0-9]` is ASCII, and `nfc` is not `nfkc`, so `３년` states nothing.

    The silence is specifically the digits: a non-breaking space between the
    number and the unit is folded by nothing and needs no folding, because
    `\\s` already admits it.

    Since #451 the `[0-9]` sentence is true of `_VALUE_MEASUREMENT` alone. The
    suppression scan reads `\\d` and does see this number, which is why
    `３년 30주` no longer names the weeks;
    `test_the_suppression_scan_reads_any_unicode_decimal_digit` is the other
    side.
    """
    from verinote.pipeline.query_measure_unit import (
        _value_measure_units,
        korean_measure_unit_mismatch,
    )

    assert _value_measure_units("３년") == ()
    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 개월인가?", "３년") is None
    # Written as the escape rather than pasted, so the non-breaking space
    # cannot be mistaken for -- or reflowed into -- an ordinary one.
    assert _value_measure_units("3\xa0년") == (("YEAR", "년"),)


@pytest.mark.parametrize(
    ("question", "value", "wrong_output", "what_the_value_really_says"),
    [
        ("샘플계약의 마감일은 몇 개월인가?", "15일 마감", ("개월", "일"), "a deadline on the 15th"),
        ("샘플계약의 기간은 몇 개월인가?", "03/15일", ("개월", "일"), "March 15th, no year"),
        ("샘플사업의 가격은 몇 원인가?", "이천만원 (15,000달러)", ("원", "달러"),
         "20 million won, in Sino-Korean numerals"),
        ("샘플사업의 기간은 몇 년인가?", "20여년 3주", ("년", "주"), "twenty-odd years"),
        ("샘플사업의 가격은 몇 원인가?", "3만여원 (15,000달러)", ("원", "달러"),
         "thirty-odd thousand won"),
        ("샘플작업의 소요시간은 몇 시간인가?", "한 시간 30분", ("시간", "분"),
         "an hour and a half, in its ordinary spelling"),
        ("샘플사업의 기간은 몇 년인가?", "반년 3주", ("년", "주"), "half a year"),
        ("샘플사업의 기간은 몇 년인가?", "5개년 계획 3주", ("년", "주"), "a five-year plan"),
        ("샘플사업의 기간은 몇 개월인가?", "6월 및 30주", ("개월", "주"), "June, or six months"),
        ("샘플사업의 기간은 몇 개월인가?", "21년", ("개월", "년"), "the year 2021, or 21 years"),
        ("샘플사업의 소요는 몇 분인가?", "2 second review", ("분", "second"), "a second review"),
        ("샘플사업의 기간은 몇 년인가?", "100주", ("년", "주"), "one hundred shares"),
        ("샘플회의의 시간은 몇 시간인가?", "5분", ("시간", "분"), "five people, honorific"),
        ("샘플계약의 마감일은 몇 개월인가?", "2021년 계약, 15일 마감", ("개월", "일"), "a deadline on the 15th"),
        ("샘플계약의 기간은 몇 개월인가?", "3월 15일~20일", ("개월", "일"), "the 15th to the 20th"),
        ("샘플계약의 기간은 몇 개월인가?", "매월 15일, 30일 정산", ("개월", "일"), "the 15th and the 30th"),
        ("샘플계약의 기간은 몇 개월인가?", "3월 15일과 20일", ("개월", "일"), "the 15th and the 20th"),
        ("샘플계약의 기간은 몇 개월인가?", "2021-03-15, 20일", ("개월", "일"), "March 15th and the 20th"),
        ("샘플회의의 시간은 몇 시간인가?", "3시 30분 ~ 45분", ("시간", "분"), "3:30 to 3:45"),
        ("샘플사업의 기간은 몇 개월인가?", "2021년 기준, 6월 및 30주", ("개월", "주"),
         "June, or six months, beside a year"),
    ],
)
def test_known_false_unit_statements_are_recorded_not_fixed(
    question, value, wrong_output, what_the_value_really_says
):
    """The caveat is wrong on these, and this records it rather than hiding it.

    These are the wrong sentences that have been found, not all the ones that
    exist -- every round of review on #445 added to the list, #450 removed the
    ones its point-in-time guard reached, #452 added to it again, and #451
    removed the three whose asked-unit quantity was a number the suppression
    scan could not spell while adding one it still cannot. What is left of that
    cause is what `_VALUE_MEASUREMENT_RELAXED` states as a rule rather than a
    list: SOME decimal digit must stand before the asked unit with nothing
    between them but digits, separators, class magnitudes and whitespace. Some
    DIGIT and not every digit -- `1경5천조원` is read on its `5` though its `1`
    is blocked, so a value is out of reach ON THIS CONDITION only when every
    digit in it fails -- it may still go unread for one of the two reasons
    `_VALUE_MEASUREMENT_RELAXED` names, which satisfy the condition and are not
    this cause. Ranged over
    occurrences of the spelling instead the same words would be false, which is
    why the noun is in the sentence and not left to the witness. `이천만원`,
    `한 시간 30분` and `반년 3주` have no digit before the unit at all -- the
    last has no numeral in it either, which is why the condition is not "a
    numeral the scan cannot spell" -- while `20여년` and `3만여원` have one
    whose gap is blocked. All five are older than #451 and untouched by it;
    successive drafts of this docstring named one class, then two, and the
    rows are here so the third cannot be left out silently again. Beside them
    sit the rows whose asked-unit SPELLING the
    table excludes on purpose, which is a different cause and is argued in
    `_MEASUREMENT_UNIT_SPELLINGS`. So this
    is a record, not a boundary, and it deliberately claims no shared cause: the
    two-digit year is not a lexical ambiguity and `second` is not Korean, so any
    argument of the form "the known ones are all X, therefore a non-X input is
    safe" is unsound on its face.

    What is true of the individual entries. `15일 마감` is a day of the month
    with no month term in front of it, and `_TIME_POINT` needs one; nothing in
    the value separates it from `15일 소요`. `03/15일` is a date with one
    separator where the ISO branch needs two, and a slashed month term was tried
    for #450 and withdrawn because it also read the numerator of a rate. `21년`
    is left reading YEAR because twenty-one years is a real duration -- the
    year+month branch takes `21년 3월` and the apostrophe branch takes `'21년`,
    and nothing separates the bare form. `2 second review` is an English ordinal
    sitting on a unit row that pays for itself elsewhere (`30 seconds` asked in
    `몇 분`). Only `100주` and `5분` need a question asked in a unit the relation
    does not really measure. `korean_measure_unit_mismatch` carries the rest.

    The rows carrying a point in time beside the misreading were silent at
    #452's parent and are wrong now, and that is not a property of those rows --
    it is true of EVERY entry above. The whole-value guard emptied the reported
    list whenever it matched anywhere, so any of these values went silent as
    soon as some other part of it happened to be a point in time, and
    span-local withdraws that cover from all of them at once.
    `2021년 계약, 15일 마감` and `2021년 기준, 6월 및 30주` are two of the
    entries wearing a date; the rest would read the same way, and adding one
    per entry would only restate the table. So do not read the dated rows as a
    class -- `_TIME_POINT` states the rule they are instances of.

    A subset of them does share a SHAPE, without sharing a fix: Korean elides
    the head of the second member of a date or clock list, so the branch takes
    the first member and the second stands outside every span. Those rows are
    chosen to straddle what the two rules measured against #452 each reach --
    `~` is reachable by a bounded continuation on the day and none of the other
    separators are; `,` is reachable by suppressing a leaked quantity whose unit
    a span already states, and `과` is not, because the head's own `15일` fails
    `_VALUE_MEASUREMENT`'s trailing lookahead; an ISO head is reachable by
    neither, since its span states no unit to repeat; and the clock is the same
    shape one branch over. A rule that fixes only one spelling cannot make this
    test green.

    Sharing a shape is not sharing a cause, and that subset does not extend the
    paragraph above: the shape is what the follow-up issue is filed on, not a
    property the rest of the list has.

    None of this is fixed here; #445 asks that a verified answer in another unit
    be caveated, not that every ambiguity be resolved. Asserting the wrong output
    is deliberate: if a later change fixes one of these, this test fails and the
    disclosure in `korean_measure_unit_mismatch` has to be corrected with it,
    instead of quietly going stale.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch(question, value) == wrong_output


@pytest.mark.parametrize(
    "value",
    ["21년 3월 ~ 22년 2월", "25년 12월 착수", "12년 6월", "2021년 3월"],
)
def test_a_year_followed_by_a_month_is_a_date_at_any_year_width(value):
    """Two-digit years are ordinary Korean notation, and were read as durations.

    `기간` asked in `몇 개월` is the issue's own headline question, so this needed
    no strained relation: a `기간` holding `21년 3월 ~ 22년 2월` was told "the
    verified value states 년", of a value that names no duration at all. The
    year+month branch reads it as the date it is.

    Delete that branch and every value here fires `(개월, 년)`.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 개월인가?", value) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("21년", ("개월", "년")),
        ("21년 계약", ("개월", "년")),
        ("10000년", ("개월", "년")),
    ],
)
def test_a_year_with_no_month_beside_it_still_reads_as_a_duration(value, expected):
    """The other half of the year+month branch: what it deliberately does NOT take.

    Widening the four-digit year branch to `[0-9]{2,4}` would silence all of
    these, and nothing in the diff used to notice. `21년` on its own really can
    be twenty-one years, so it keeps reading YEAR and is disclosed as a wrong
    sentence when it is not; that is the trade, and this is the assertion that
    makes reversing it fail rather than pass.

    `10000년` is here for a second reason: it also pins that the year+month
    branch did not acquire a way to match a five-digit run.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 개월인가?", value) == expected


@pytest.mark.parametrize("value", ["12년 6개월", "2년 3개월", "1년 6개월"])
def test_a_duration_written_with_개월_is_not_swallowed_by_the_year_month_branch(value):
    """The counter is what separates a duration from a date, not the number.

    `12년 6개월` is twelve years and six months and has the same digits as a date
    would; what makes it a duration is `개월` rather than bare `월`. All three
    state months, so the same-unit suppression is what silences them -- if the
    year+month branch had swallowed them they would be silent for the wrong
    reason, which the unit list below the assertion distinguishes.
    """
    from verinote.pipeline.query_measure_unit import (
        _value_measure_units,
        korean_measure_unit_mismatch,
    )

    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 개월인가?", value) is None
    assert ("MONTH", "개월") in _value_measure_units(value)


def test_the_iso_branch_no_longer_silences_the_durations_beside_a_date():
    """The cost #452 took back, recorded where the branch's cost used to be.

    `2021.03.15 일` is a date with a unit spelling on the end, and without the
    branch it is reported as stating days -- that is what the branch is for.
    Under the whole-value guard the same branch also silenced a genuine duration
    stated next to a date, and both values below were silent. Span-local drops
    only the quantity overlapping the date, so each duration is reported.

    Note which assertion is sensitive to what. The two flipped rows record the
    recovery and are a statement about the guard, not about the branch: with the
    ISO branch deleted they read the same. What the branch itself decides is
    `2021-03-153주`, whose digit run begins inside the date -- `_VALUE_MEASUREMENT`
    reads `153주` from the date's own `15` -- so the whole quantity overlaps the
    span and is dropped. Delete the branch and that value is caveated `(개월, 주)`.
    The branch's other killer is `2021.03.15 일` in
    `test_each_time_point_branch_is_needed_by_one_of_these_values`.
    """
    from verinote.pipeline.query_measure_unit import (
        _value_measure_units,
        korean_measure_unit_mismatch,
    )

    question = "샘플계약의 기간은 몇 개월인가?"
    assert korean_measure_unit_mismatch(question, "2024/01/02 3주") == ("개월", "주")
    assert korean_measure_unit_mismatch(question, "2021-03-15 (3일)") == ("개월", "일")
    # Branch-sensitive: the quantity starts inside the date and is dropped whole.
    assert korean_measure_unit_mismatch(question, "2021-03-153주") is None
    # The durations really are there, and now they are read.
    assert _value_measure_units("3주") == (("WEEK", "주"),)
    assert _value_measure_units("(3일)") == (("DAY", "일"),)


def test_a_single_digit_year_before_a_month_is_left_alone():
    """The year+month branch's lower bound, which is a judgement, not a fact.

    `[0-9]{2,4}` takes `21년 3월` and leaves `2년 3월`. Both readings of the
    latter are rare -- a duration would normally be written `2년 3개월`, and a
    date `2년 3월` only makes sense in a relative or fiscal year -- so neither
    side is clearly right and the rule takes the conservative one, changing as
    little as possible. This records which side that is, so widening the bound
    to `[0-9]{1,4}` fails here rather than passing quietly.

    The day branch's year prefix is the same width for the same reason, and it
    makes the same judgement: `2년 3월 15일` and `1년 3월 15일` have their date
    covered from the month term on and their one-digit year left outside the
    span, where it is read as a duration. Both digits are asserted, so the shape
    does not read as if `2년` exhausted it. This is the only place a component
    that could belong to the date's own notation is deliberately left out of the
    span.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 개월인가?", "2년 3월") == (
        "개월",
        "년",
    )
    for value in ["2년 3월 15일", "1년 3월 15일"]:
        assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 개월인가?", value) == (
            "개월",
            "년",
        ), value


@pytest.mark.parametrize(
    ("question", "value"),
    [
        ("샘플작업의 소요시간은 몇 시간인가?", "3시간30분"),
        ("샘플작업의 소요시간은 몇 시간인가?", "3시간 30분"),
        ("샘플사업의 기간은 몇 년인가?", "2년6개월"),
        ("샘플사업의 기간은 몇 년인가?", "2년 6개월"),
        ("샘플사업의 기간은 몇 주인가?", "1주2일"),
        ("샘플사업의 기간은 몇 개월인가?", "2년 6개월의 사업기간"),
        ("샘플사업의 기간은 몇 개월인가?", "2개월10일"),
    ],
)
def test_a_quantity_in_the_asked_unit_suppresses_the_caveat_however_it_is_spaced(
    question, value
):
    """Spacing must not decide whether a correct answer gets a wrong caveat.

    `_VALUE_MEASUREMENT`'s trailing lookahead refuses a unit run into the next
    character, which is right for reading what a value states and wrong for
    asking whether the value already carries the unit the question wanted. It
    hid the asked unit in every unspaced case here, and the caveat fired: a
    reader asking `몇 시간인가?` of `3시간30분` was told the value states minutes
    and that verinote applies no conversion, when the leading quantity is
    exactly the hours asked for. `3시간 30분`, one space apart, was silent.

    The spaced and unspaced forms are paired deliberately, so a regression that
    reintroduces the asymmetry fails on the pair rather than on a single case.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch(question, value) is None


def test_the_suppression_scan_does_not_read_a_shorter_spelling_inside_a_longer():
    """Why the suppression scan is longest-first rather than lookahead-free.

    Dropping the lookahead is what lets `3시간30분` suppress, but dropping it
    without reordering would let a months question find `달` inside `달러` and
    suppress a caveat the value never earned. The alternation is sorted longest
    spelling first, so `달러` is taken before `달`.

    The second assertion is the one that would fail under a bare substring test
    or a per-unit scan with no ordering: the value states dollars and weeks, the
    question asked months, and the weeks caveat must survive.
    """
    from verinote.pipeline.query_measure_unit import (
        _value_states_asked_unit,
        korean_measure_unit_mismatch,
    )

    assert _value_states_asked_unit("3달러", "MONTH") is False
    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 개월인가?", "3달러, 2주 소요") == (
        "개월",
        "주",
    )
    assert _value_states_asked_unit("2주일", "WEEK") is True
    assert _value_states_asked_unit("3시간30분", "HOUR") is True


def test_the_suppression_scan_sees_everything_the_value_scan_sees():
    """The relaxed scan is a superset, so it subsumes the equality test it replaced.

    If some unit could be read by `_value_measure_units` and missed by
    `_value_states_asked_unit`, a value stating the asked unit plainly would
    stop suppressing and the caveat would fire on it. Swept over every spelling
    against a set of following characters that exercise the lookahead.
    """
    from verinote.pipeline.query_measure_unit import (
        _MEASUREMENT_UNIT_SPELLINGS,
        _value_measure_units,
        _value_states_asked_unit,
    )

    # The number set carries a magnitude word deliberately. Without one, the
    # relaxed pattern could lose its magnitude run and this property would still
    # hold, which is how `3만 원, 5달러` reached `('원', '원')` unnoticed. The
    # last three are the range #451 added to that run and to the digit class.
    values = [
        f"{number}{spelling}{tail}"
        for number in ("1", "3", "1000", "3만", "1억", "2천", "2천만", "2백", "１")
        for spelling in _MEASUREMENT_UNIT_SPELLINGS
        for tail in ("", " ", "차", "5", "의", "간", "러", "s")
    ]
    for value in values:
        for unit, _ in _value_measure_units(value):
            assert _value_states_asked_unit(value, unit), (value, unit)


@pytest.mark.parametrize("value", ["21.03.15일", "25-01-15일", "2021.03.15 일"])
def test_the_iso_branch_reads_a_two_digit_year_like_every_other_branch(value):
    """The year+month branch takes two-digit years; the ISO branch now does too.

    Holding this branch at four digits while the branch above it accepted two --
    on the stated grounds that two-digit years are ordinary Korean notation --
    was the same premise accepted in one place and refused in the next, and
    `21.03.15일` was read as fifteen days because of it.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 개월인가?", value) is None


@pytest.mark.parametrize("value", ["2.03.15일", "12021.03.15일"])
def test_the_iso_year_is_bounded_below_and_on_the_left(value):
    """Both new bounds on the ISO year, pinned the way branch 2's are.

    `2.03.15일` has a one-digit year and `12021.03.15일` a five-digit run; widen
    to `{1,4}` or drop the `(?<![0-9])` and each becomes a date, silencing a
    value this rule otherwise reads. Both are contrived -- that is the point of
    a bound -- but an unpinned bound is one a later change removes for free.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 개월인가?", value) == (
        "개월",
        "일",
    )


@pytest.mark.parametrize("suffix", ["간", "가량", "정도", "쯤", "짜리"])
def test_every_unit_suffix_alternative_has_a_value_that_needs_it(suffix):
    """One value per member of `_UNIT_SUFFIX`, so no member rides free.

    Four of the five were unpinned: dropping `가량`, `정도`, `쯤` or `짜리` left
    the suite green while changing what values read. Same argument as
    `_ROW_FIXTURES` two tables over -- an unpinned entry licences a silent
    deletion -- and the same remedy.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 년인가?", f"3개월{suffix}") == (
        "년",
        "개월",
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1000엔", ("원", "엔")), ("1000 yen", ("원", "yen")),
     ("500유로", ("원", "유로")), ("500 euro", ("원", "euro"))],
)
def test_the_yen_and_euro_family_rows_are_pinned(value, expected):
    """`_MEASUREMENT_FAMILY`'s JPY and EUR rows, the two that were unpinned.

    Eleven of the thirteen rows were already killed by existing fixtures; these
    two were reachable only through `_ROW_FIXTURES`, which checks
    `_value_measure_units` and never consults the family table. Remap either to
    another family and the cross-currency caveat here goes silent.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch("샘플사업의 가격은 몇 원인가?", value) == expected


def test_the_year_month_branch_is_bounded_above_and_on_the_left():
    """Branch 2's `{2,4}` upper bound and its `(?<![0-9])`, both pinned at once.

    `10000년 3월` is a five-digit run followed by a month. Widen the branch to
    `{2,}` and it matches outright; drop the left-hand lookbehind and it matches
    on the inner `0000년 3월`. Either way the value becomes a date and stops
    being read, so one value covers both bounds.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 개월인가?", "10000년 3월") == (
        "개월",
        "년",
    )


def _relaxed_pattern_from(number):
    """The shipped relaxed pattern with its number replaced, for the mutants.

    Built from the live spellings table, the live sort and the live
    `_UNIT_SHADOW_GUARD`, so a mutant differs from the shipped pattern in its
    number and in nothing else. The guard is taken from the module rather than
    rebuilt here for the same reason: a copy would let a mutant differ in two
    places at once and the callers below would stop measuring their own subject.

    Reading a part live is not enough on its own -- a part could be dropped from
    this rebuild entirely and every caller would go on comparing a mutant against
    a pattern that is no longer the shipped one, quietly.
    `test_the_pattern_rebuilds_are_still_the_shipped_pattern` is what closes
    that, and it lives outside these helpers on purpose: the check has to read
    `_VALUE_MEASUREMENT_RELAXED`, and a caller can be holding that monkeypatched
    to a mutant at the moment it calls -- the second `monkeypatch.setattr` in
    `test_the_shadow_guard_gains_no_caveat_on_a_word_it_only_prefixes` evaluates
    its argument while the first patch is live. One such caller is enough to
    make an in-helper assert compare the rebuild against a mutant.
    """
    import re

    from verinote.pipeline.query_measure_unit import (
        _MEASUREMENT_UNIT_SPELLINGS,
        _UNIT_SHADOW_GUARD,
    )

    return re.compile(
        number
        + _UNIT_SHADOW_GUARD
        + r"(?P<unit>"
        + "|".join(
            re.escape(s) for s in sorted(_MEASUREMENT_UNIT_SPELLINGS, key=len, reverse=True)
        )
        + r")"
    )


_HEAD_RELAXED_NUMBER = r"[0-9][0-9,.]*\s*[만억천조]?\s*"
"""The relaxed number as it stood before #451, for the tests that compare.

Written out rather than derived, because what it is for is to be the OTHER
pattern: deriving it from `_RELAXED_QUANTITY_NUMBER` would make it follow the
live constant and the comparisons below would compare a thing with itself.
"""


def test_an_unreadable_asked_unit_number_no_longer_names_the_neighbour():
    """#451: the four witnesses whose asked-unit number the scan could not spell.

    Each was told the value states a neighbouring unit while its own leading
    figure was exactly what the question asked for. The neighbour assertion is
    the non-vacuity half: without it a value that stopped stating anything at
    all -- because the reporting scan went blind rather than because the
    suppression scan learned to read -- would pass this test.

    `1억원 (15,000달러)` is the control. It was already silent, through the one
    magnitude word both patterns have always read, so its staying silent shows
    the fix did not arrive by breaking that path.
    """
    from verinote.pipeline.query_measure_unit import (
        _value_measure_units,
        korean_measure_unit_mismatch,
    )

    for question, value, neighbour in [
        ("샘플사업의 가격은 몇 원인가?", "2천만원 (15,000달러)", "달러"),
        ("샘플사업의 가격은 몇 원인가?", "1억5천만원 및 20,000달러", "달러"),
        ("샘플사업의 가격은 몇 원인가?", "2백만원 (15,000달러)", "달러"),
        ("샘플사업의 기간은 몇 년인가?", "３년 30주", "주"),
    ]:
        assert korean_measure_unit_mismatch(question, value) is None, value
        assert neighbour in {s for _, s in _value_measure_units(value)}, value

    assert korean_measure_unit_mismatch("샘플사업의 가격은 몇 원인가?", "1억원 (15,000달러)") is None


def test_the_suppression_scan_reads_a_run_of_magnitude_words(monkeypatch):
    """`2천만원` stacks two magnitudes, and the scan reads through both.

    The killer is derived from the live constant rather than retyped: turn the
    run back into the single optional character it was before #451 and both
    stacked witnesses name the dollars again.

    Both are asserted because the issue reports both, not because either is an
    independent killer -- they are not. `1억5천만원` ends in `5천만원`, so no
    magnitude class or run length separates the two: under every widening
    tried, they move together. The second row is a record of what #451 was
    filed on, and the test says so rather than implying a discrimination it
    does not make.

    The reporting scan is deliberately left where it was, so `2천만원` still
    STATES nothing -- what changed is only whether the value is read as already
    carrying the unit that was asked for.
    """
    import verinote.pipeline.query_measure_unit as qmu
    from verinote.pipeline.query_measure_unit import (
        _RELAXED_QUANTITY_NUMBER,
        _value_measure_units,
        _value_states_asked_unit,
        korean_measure_unit_mismatch,
    )

    assert _value_states_asked_unit("2천만원", "KRW") is True
    assert _value_states_asked_unit("1억5천만원", "KRW") is True
    assert _value_measure_units("2천만원") == ()

    one_magnitude = _RELAXED_QUANTITY_NUMBER.replace(r")*", r")?")
    assert one_magnitude != _RELAXED_QUANTITY_NUMBER, "the mutation did not apply"
    monkeypatch.setattr(
        qmu, "_VALUE_MEASUREMENT_RELAXED", _relaxed_pattern_from(one_magnitude)
    )
    question = "샘플사업의 가격은 몇 원인가?"
    assert korean_measure_unit_mismatch(question, "2천만원 (15,000달러)") == ("원", "달러")
    assert korean_measure_unit_mismatch(question, "1억5천만원 및 20,000달러") == ("원", "달러")


def test_the_suppression_scan_reads_any_unicode_decimal_digit(monkeypatch):
    """`３년` is a number, and the suppression scan admits any Unicode decimal digit.

    `\\d` rather than a listed range, which is the claim the Arabic-Indic and
    Devanagari assertions make -- the first two below: a `[0-9０-９]` would
    satisfy the full-width witness and fail those. The reporting scan is
    unchanged and still reads ASCII digits only.

    Decimal digit and not numeral: `\\d` is the Nd category, so `一년` is no more
    readable here than `이천만원` is, and the numeral axis stays where
    `korean_measure_unit_mismatch` records it.

    The killer narrows the class back to ASCII, and does it by replacing the
    head as one substring -- replacing `\\d` alone would leave the nested set
    `[[0-9],.]` and a `FutureWarning` rather than the pattern intended.
    """
    import verinote.pipeline.query_measure_unit as qmu
    from verinote.pipeline.query_measure_unit import (
        _RELAXED_QUANTITY_NUMBER,
        _value_measure_units,
        _value_states_asked_unit,
        korean_measure_unit_mismatch,
    )

    assert _value_states_asked_unit("٣년", "YEAR") is True
    assert _value_states_asked_unit("३년", "YEAR") is True
    assert _value_measure_units("３년") == ()

    ascii_only = _RELAXED_QUANTITY_NUMBER.replace(r"\d[\d,.]*", r"[0-9][0-9,.]*")
    assert ascii_only != _RELAXED_QUANTITY_NUMBER and "\\d" not in ascii_only
    monkeypatch.setattr(
        qmu, "_VALUE_MEASUREMENT_RELAXED", _relaxed_pattern_from(ascii_only)
    )
    assert korean_measure_unit_mismatch("샘플사업의 기간은 몇 년인가?", "３년 30주") == (
        "년",
        "주",
    )


def test_the_widened_number_can_only_silence(monkeypatch):
    """Reading more here ends caveats and cannot start one, premise and outcome.

    The premise is the load-bearing half, and it is a tripwire rather than a
    sample: `korean_measure_unit_mismatch` returns None when this scan is true
    and is otherwise a function of `_value_measure_units` alone, so the only way
    a wider number changes an answer is by reading FEWER units. It cannot,
    because no unit spelling begins with a character the number itself admits --
    so no spelling occurrence can start inside a number, no number can extend
    across one, and at every start where the narrower pattern matched the wider
    one matches the same span and unit. Add a spelling that opens with a digit,
    a comma, a dot, whitespace or a magnitude word and the argument fails here
    rather than silently.

    The premise is asserted in two halves because a literal set cannot state
    it. `[\\s\\d]` is the number's own two open classes, and neither is
    enumerable by hand: `\\d` is every Unicode decimal digit rather than the
    ASCII ten, and `\\s` reaches `\\r`, `\\f`, `\\v` and the non-breaking space,
    which a hand-typed `" \\t\\n"` would miss. The literal half is the closed
    part -- the comma, the dot, and the magnitude class read live.

    The outcome half sweeps the two patterns against each other through
    `_value_states_asked_unit` itself rather than a copy of it. The corpus
    carries the shapes where a longer match could hide a later one, since
    `finditer` resumes from a match's end.
    """
    import re

    import verinote.pipeline.query_measure_unit as qmu
    from verinote.pipeline.query_measure_unit import (
        _MEASUREMENT_FAMILY,
        _MEASUREMENT_UNIT_SPELLINGS,
        _SINO_KOREAN_MAGNITUDES,
        _value_states_asked_unit,
    )

    literal_prefix_chars = set(",.") | set(_SINO_KOREAN_MAGNITUDES)
    assert [
        s for s in _MEASUREMENT_UNIT_SPELLINGS if s[0] in literal_prefix_chars
    ] == []
    assert [
        s for s in _MEASUREMENT_UNIT_SPELLINGS if re.fullmatch(r"[\s\d]", s[0])
    ] == []

    corpus = [
        f"{number}{spelling}{tail}"
        for number in ("3", "1000", "3만", "2천만", "1억5천만", "2백", "5십", "３", "１，０００")
        for spelling in _MEASUREMENT_UNIT_SPELLINGS
        for tail in ("", " ", "차", "5", "의", "간", "러", "s", " 3주", "2주")
    ] + [
        # The swallow shapes: a longer match here could end past a unit the
        # narrower pattern reached on its own.
        "1억 2주", "3만 5년6주", "2천 3주", "3만 5천원", "1억 2년 3주", "3만 원, 5달러",
        "1조5천억원 3달러", "2백 3주", "5십 2년", "3천조억만원 2주", "1경5천조원 3달러",
        "２천만원 (15,000달러)", "1억5천만원 및 20,000달러", "3시간30분", "２분기 실적, 2시간 소요",
    ]
    units = sorted(set(_MEASUREMENT_FAMILY))

    def sweep():
        return [
            [_value_states_asked_unit(value, unit) for unit in units]
            for value in corpus
        ]

    shipped = sweep()
    monkeypatch.setattr(
        qmu,
        "_VALUE_MEASUREMENT_RELAXED",
        _relaxed_pattern_from(_HEAD_RELAXED_NUMBER),
    )
    head = sweep()

    lost = [
        (value, unit)
        for value, head_row, shipped_row in zip(corpus, head, shipped)
        for unit, before, after in zip(units, head_row, shipped_row)
        if before and not after
    ]
    assert lost == []
    gained = sum(
        1
        for head_row, shipped_row in zip(head, shipped)
        for before, after in zip(head_row, shipped_row)
        if after and not before
    )
    assert gained > 0, "the shipped scan must read strictly more somewhere"


def test_the_magnitude_run_needs_no_inner_digits():
    """The declined alternative reads the same units, measured rather than assumed.

    `(?:[...]\\s*[\\d,.]*\\s*)*` reads `1억5천만원` from the `1` where the shipped
    run reads it from the `5`, and both reach the same unit: a stacked number is
    a digit run, then magnitude words possibly separated by further digit runs,
    then the unit, so the shipped form starts at the last inner run and the
    region the longer form swallows holds no unit to hide.

    That last clause is the premise, and it is re-derived from the live table
    and the live class rather than restated, so adding `경` to
    `_SINO_KOREAN_MAGNITUDES` re-derives it too. Written with `startswith` and
    not by indexing, which raises rather than answers if the table ever holds
    an empty key.

    The two patterns really are different, which the match starts show. Without
    that check the equality could hold because the mutation did nothing.
    """
    from verinote.pipeline.query_measure_unit import (
        _MEASUREMENT_UNIT_SPELLINGS,
        _SINO_KOREAN_MAGNITUDES,
        _VALUE_MEASUREMENT_RELAXED,
    )

    assert [
        s
        for s in _MEASUREMENT_UNIT_SPELLINGS
        if s.startswith(tuple(_SINO_KOREAN_MAGNITUDES))
    ] == []

    inner_digits = _relaxed_pattern_from(
        r"\d[\d,.]*\s*(?:[" + _SINO_KOREAN_MAGNITUDES + r"]\s*[\d,.]*\s*)*"
    )
    assert inner_digits.pattern != _VALUE_MEASUREMENT_RELAXED.pattern
    corpus = [
        f"{number}{spelling}{tail}"
        for number in ("1억5천만", "3만 5천", "2억 5,000만", "2백만", "3천5백만", "5백50만", "1경5천조")
        for spelling in _MEASUREMENT_UNIT_SPELLINGS
        for tail in ("", " ", " 3주", "5", "차")
    ]
    starts_differ = 0
    for value in corpus:
        assert [m.group("unit") for m in inner_digits.finditer(value)] == [
            m.group("unit") for m in _VALUE_MEASUREMENT_RELAXED.finditer(value)
        ], value
        if [m.start() for m in inner_digits.finditer(value)] != [
            m.start() for m in _VALUE_MEASUREMENT_RELAXED.finditer(value)
        ]:
            starts_differ += 1
    assert starts_differ > 0, "the two patterns must differ, or the equality is vacuous"


def test_the_magnitude_class_is_a_series_and_this_is_where_it_stops():
    """The residue of `_SINO_KOREAN_MAGNITUDES`, re-derived rather than restated.

    `백` is in, so `2백만원` and `3천5백만원` suppress -- and note that neither
    of those demonstrates `십`, which is the pairing this docstring made when it
    was first written. A stated member illustrated by a value that does not
    contain it is a member with no evidence behind it, and `십` could be deleted
    with the whole suite green until the per-member block below was added. Its
    own values are `5십원` and `2백5십원`.

    `경` is the known next member and is out, so a sum written with it flush
    against the unit is still told it states the dollars. Add `경` to the class
    and the `1경원` assertion fails, which is what forces the boundary in that
    constant's docstring to be corrected with the code -- the tripwire is the
    whole protection here, because an unrecognised magnitude on this scan leaves
    a wrong sentence standing rather than costing a caveat.

    The last two rows are what makes the residue statable rather than open, and
    they are the pair a reader is likeliest to get wrong. The run starts at the
    LAST digit run and must reach the unit without interruption, so
    `1경5천조원` is read -- its last digit run is the `5`, and only `천` and `조`
    follow -- while `1천경원` is not, even though its `천` is in the class. What
    decides is the whole gap between the final digit and the unit, not whether
    some member of it is known.

    Every member of the class carries its own row, because a stated member
    nothing pins is a member that can be deleted with the suite green -- which
    `십` was until this test was written. The values are chosen so that removing
    one character from `_SINO_KOREAN_MAGNITUDES` fails a row naming it and no
    other: `5십원` needs `십` and nothing else, `2백원` needs `백`, `3천원`
    `천`, `3만원` `만`, `1억원` `억`, `1조원` `조`. Verified as a diagonal, one
    deletion at a time.

    `2백5십원` is the shape a sum below ten thousand is actually written in, and
    it is here as a reading rather than as a seventh killer: it is lost by
    dropping `십` and NOT by dropping `백`, because the run reads from the last
    magnitude it knows and the `5십` is reachable on its own. That is the same
    mechanism the `1경5천조원` row above turns on, which is why one value cannot
    stand for two members.
    """
    from verinote.pipeline.query_measure_unit import (
        _SINO_KOREAN_MAGNITUDES,
        korean_measure_unit_mismatch,
    )

    question = "샘플사업의 가격은 몇 원인가?"
    assert korean_measure_unit_mismatch(question, "2백만원 (15,000달러)") is None
    assert korean_measure_unit_mismatch(question, "3천5백만원 (15,000달러)") is None
    assert korean_measure_unit_mismatch(question, "1경원 (15,000달러)") == ("원", "달러")
    assert korean_measure_unit_mismatch(question, "1경5천조원 (15,000달러)") is None
    assert korean_measure_unit_mismatch(question, "1천경원 (15,000달러)") == ("원", "달러")

    # One value per member, so no member rides free. Derived from the live
    # constant rather than listed, so a member added without a value here fails
    # for want of a fixture instead of passing unpinned.
    per_member = {
        "십": "5십원 (15,000달러)",
        "백": "2백원 (15,000달러)",
        "천": "3천원 (15,000달러)",
        "만": "3만원 (15,000달러)",
        "억": "1억원 (15,000달러)",
        "조": "1조원 (15,000달러)",
    }
    assert set(per_member) == set(_SINO_KOREAN_MAGNITUDES)
    for member, value in per_member.items():
        assert korean_measure_unit_mismatch(question, value) is None, member
    assert korean_measure_unit_mismatch(question, "2백5십원 (15,000달러)") is None


def test_the_reporting_scans_magnitude_bound_has_two_halves_and_both_are_pinned():
    """`[만억천조]?` says "at most one" AND "only these four"; pin each separately.

    `korean_measure_unit_mismatch` states both halves in one sentence, and one
    fixture cannot hold them. The obvious choice is vacuous: `2백만원` needs the
    run AND the class, so it stays unreadable under a mutation that supplies
    only one, and a test resting on it would pass while either half rotted.

    The diagonal below is the whole point. `2천만원` needs only the run, so it
    moves under the run mutation and not the class one; `2백원` and `5십원` need
    only the class, so they move under the class mutation and not the run one.
    Each half therefore has a value that fails for it alone. `2백만원` is
    asserted too, but as the conjunction it names rather than as a killer.

    #451 sits on the class half: the SUPPRESSION scan gained `십백` and this one
    did not, which is why `2백원` asked in won is silent here and suppresses
    there.
    """
    import re

    from verinote.pipeline.query_measure_unit import _VALUE_MEASUREMENT, _value_measure_units

    for value in ["2천만원", "2백원", "5십원", "2백5십원", "2백만원"]:
        assert _value_measure_units(value) == (), value
    # The control: one magnitude from the four is read, so the silences above
    # are the bound and not a blind pattern.
    assert _value_measure_units("3만원") == (("KRW", "원"),)

    live = _VALUE_MEASUREMENT.pattern
    bound = "[만억천조]?"
    assert bound in live, "the bound moved; this test hardcodes only its text"
    wider_class = re.compile(live.replace(bound, "[십백천만억조]?"))
    longer_run = re.compile(live.replace(bound, "[만억천조]*"))

    def reads(pattern, value):
        return [m.group("unit") for m in pattern.finditer(value)]

    # Widening the class reaches the sub-myriad values and not the stacked one.
    assert reads(wider_class, "2백원") == ["원"]
    assert reads(wider_class, "5십원") == ["원"]
    assert reads(wider_class, "2천만원") == []
    # Allowing a run reaches the stacked value and not the sub-myriad ones.
    assert reads(longer_run, "2천만원") == ["원"]
    assert reads(longer_run, "2백원") == []
    assert reads(longer_run, "5십원") == []
    # And the conjunction really is one: neither mutation alone reads it.
    assert reads(wider_class, "2백만원") == []
    assert reads(longer_run, "2백만원") == []


def test_a_separator_is_admitted_only_inside_the_leading_digit_run(monkeypatch):
    """`2천만,년` states nothing, and that is the number's shape, not luck.

    `_RELAXED_QUANTITY_NUMBER` is `\\d[\\d,.]*` followed by a run of magnitude
    words, so the comma and the dot are admitted inside the leading digit run
    and nowhere else. A separator standing AFTER a magnitude word ends the
    match, which is why `2천만,년` is not read as years -- even though a digit
    stands before the `년` with only digits, separators and magnitudes between
    them, and so the value satisfies the rule
    `_VALUE_MEASUREMENT_RELAXED` states.

    That is the whole reason this test exists. That rule is stated as necessary
    and not sufficient, and it names two classes where the converse fails; this
    is the second of them. Until this fixture, nothing held it: admitting
    `[,.]` after the magnitude group left the entire file green, so the
    disclosure was a reading of the constant rather than a claim anything would
    notice losing. The other class has
    `test_달러_is_the_only_prefix_pair_that_crosses_canonical_units`; this is
    the matching tripwire.

    The killer is derived from the live constant rather than retyped, so it
    follows the number if the number moves.
    """
    import verinote.pipeline.query_measure_unit as qmu
    from verinote.pipeline.query_measure_unit import (
        _RELAXED_QUANTITY_NUMBER,
        _VALUE_MEASUREMENT_RELAXED,
        _value_states_asked_unit,
        korean_measure_unit_mismatch,
    )

    question = "샘플사업의 기간은 몇 년인가?"
    assert _value_states_asked_unit("2천만,년", "YEAR") is False
    assert _value_states_asked_unit("2천만.년", "YEAR") is False
    assert korean_measure_unit_mismatch(question, "2천만,년 3주") == ("년", "주")
    # The minimal pair: this differs from the row above it by the comma alone,
    # so the two together fail when the POSITION moves and not when the value
    # stops being read for some other cause. On its own the `False` above would
    # also go green if `년` ever left the spellings table, and the pin would
    # survive having stopped meaning anything.
    assert _value_states_asked_unit("2천만년", "YEAR") is True
    # The control, and the half of the claim that is about POSITION. Asserted on
    # the match and not on the boolean: drop `[,.]` from the leading run and
    # `1,000년` is still read, because the scan resumes and finds `000년`, so no
    # `_value_states_asked_unit` assertion here could notice. The span is what
    # moves -- `1,000년` becomes `000년`.
    assert _VALUE_MEASUREMENT_RELAXED.search("1,000년").group(0) == "1,000년"
    assert _value_states_asked_unit("1,000년", "YEAR") is True
    assert korean_measure_unit_mismatch(question, "1,000년 3주") is None

    after_magnitudes = _RELAXED_QUANTITY_NUMBER.replace(r"]\s*)*", r"]\s*[,.]*\s*)*")
    assert after_magnitudes != _RELAXED_QUANTITY_NUMBER, "the mutation did not apply"
    monkeypatch.setattr(
        qmu,
        "_VALUE_MEASUREMENT_RELAXED",
        _relaxed_pattern_from(after_magnitudes),
    )
    assert _value_states_asked_unit("2천만,년", "YEAR") is True
    assert korean_measure_unit_mismatch(question, "2천만,년 3주") is None


@pytest.mark.parametrize("value", ["3월 15일", "3월  15일", "3월\t15일", "3월15일"])
def test_whitespace_between_a_digit_month_and_its_day_still_reads_as_a_date(value):
    """The day branch is `\\s*` on both sides, so whitespace does not separate them.

    The bullet describing this said "immediately beside" while its own example
    carried a space. Since #450 a month part may stand between the two as well,
    so `3월 중 15일` is a date now; what still ends the match is a word outside
    `_MONTH_PART_MEMBERS`, which is pinned separately.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch("샘플계약의 기간은 몇 개월인가?", value) is None


def test_the_suppression_scan_keeps_the_digit_head_of_the_strict_pattern():
    """The relaxed pattern's twin of "the whole precision of this rule".

    `_VALUE_MEASUREMENT`'s leading `[0-9]` is pinned by a 637-pair prose sweep,
    and the relaxed pattern requires a digit in the same position with nothing
    guarding it. The two heads differ in their digit CLASS and in nothing else,
    `\\d` there against `[0-9]` here; what differs elsewhere in the two numbers
    is the magnitude group, which is not what this test is about.
    Drop the head there and the bare `원` inside `지원` counts as a quantity in
    won, so this value stops warning about its dollars -- the same prose-noise
    hazard, arriving through the suppression side instead.
    """
    from verinote.pipeline.query_measure_unit import (
        _value_states_asked_unit,
        korean_measure_unit_mismatch,
    )

    assert _value_states_asked_unit("지원 없음, 3달러", "KRW") is False
    assert korean_measure_unit_mismatch("샘플사업의 가격은 몇 원인가?", "지원 없음, 3달러") == (
        "원",
        "달러",
    )


def test_the_suppression_scan_reads_at_least_the_value_scans_magnitude_word():
    """The relaxed scan must read at least what the value scan does, or it lies.

    Since #451 the two do not read the same magnitude word, nor the same
    magnitude class: the relaxed number is a run over
    `_SINO_KOREAN_MAGNITUDES` and `_VALUE_MEASUREMENT` still takes one
    `[만억천조]`. What is required is the inequality, not the equality. Drop the
    magnitude run from the relaxed pattern and the two disagree about `3만 원`
    in the unsafe direction: the reporting scan reads won, the suppression scan
    does not, so nothing suppresses and the first same-family unit reported is
    the won itself. The caveat then renders as "the question's counter is 원;
    the verified value states 원" -- a sentence that contradicts itself in front
    of the reader. The other direction is safe by construction, and
    `test_the_widened_number_can_only_silence` is where it is argued.
    """
    from verinote.pipeline.query_measure_unit import (
        _value_measure_units,
        _value_states_asked_unit,
        korean_measure_unit_mismatch,
    )

    assert _value_measure_units("3만 원, 5달러") == (("KRW", "원"), ("USD", "달러"))
    assert _value_states_asked_unit("3만 원, 5달러", "KRW") is True
    assert korean_measure_unit_mismatch("샘플사업의 가격은 몇 원인가?", "3만 원, 5달러") is None


_LOST_CAVEAT_ROWS = [
    ("샘플사업의 기간은 몇 주인가?", "2천만주 보유, 3개월 준비", "개월", "one magnitude"),
    ("샘플사업의 기간은 몇 주인가?", "3백주 보유, 3개월 준비", "개월", "no 백"),
    ("샘플사업의 기간은 몇 주인가?", "１００주 보유, 3개월 준비", "개월", "ASCII digits"),
    ("샘플사업의 소요는 몇 분인가?", "3분기실적, 2시간 소요", "시간", "no right bound"),
]
"""The silences this scan still costs, each with the narrowing that would end it.

The fourth field is what makes the list a diagonal rather than four examples,
and it is a claim about the whole table: no two rows share a cause. Hoisted out
of the `parametrize` so the test can read the other rows while checking its own
column.
"""


def _lost_caveat_narrowings():
    """The four narrowings `_LOST_CAVEAT_ROWS` is a diagonal against.

    Three replace the number and one replaces the guard, each derived from the
    live constant and each asserting that its own substitution changed
    something, so a mutant that quietly stopped being a mutant is loud. Built
    through `_relaxed_pattern_from` and `_relaxed_pattern_with_guard`, so every
    part except the one under test is the shipped one.
    """
    from verinote.pipeline.query_measure_unit import _RELAXED_QUANTITY_NUMBER

    numbers = {
        "one magnitude": _RELAXED_QUANTITY_NUMBER.replace(r")*", r")?"),
        "no 백": _RELAXED_QUANTITY_NUMBER.replace("백", ""),
        "ASCII digits": _RELAXED_QUANTITY_NUMBER.replace(
            r"\d[\d,.]*", r"[0-9][0-9,.]*"
        ),
    }
    for name, number in numbers.items():
        assert number != _RELAXED_QUANTITY_NUMBER, f"{name} did not apply"
    built = {name: _relaxed_pattern_from(n) for name, n in numbers.items()}
    # `_unbounded_shadow_guard` asserts its own substitution applied.
    built["no right bound"] = _relaxed_pattern_with_guard(_unbounded_shadow_guard())
    return built


@pytest.mark.parametrize(
    ("question", "value", "unit_the_caveat_used_to_name", "narrowing_that_fires_it"),
    _LOST_CAVEAT_ROWS,
)
def test_caveats_lost_to_the_suppression_scan_are_recorded_not_fixed(
    question, value, unit_the_caveat_used_to_name, narrowing_that_fires_it, monkeypatch
):
    """What the generous suppression reading still costs, priced rather than implied.

    Two causes, and the second is not the first one narrowed.

    The first three rows are a spelling that means the other thing with NOTHING
    appended. `주` is the counter for shares, so `3백주` is three hundred shares
    beside a genuine three months, and there is no longer word for
    `_UNIT_SHADOW_WORDS` to hold; #467 is where that half is filed.

    The fourth is what `_UNIT_SHADOW_GUARD`'s right bound gives up.
    `3분기실적` continues its `분기` flush with another Hangul letter, so the
    guard declines to refuse the spelling there, and that is the same decision
    that keeps `30분기준 회의, 2시간 소요` -- thirty minutes taken as a basis --
    from being caveated against a value that really does state thirty minutes.

    Each value here states a real same-family mismatch somewhere else and each is
    silent because of it. The last parameter records the caveat that used to be
    shown, and the second assertion re-derives it from `_value_measure_units`
    rather than taking this docstring for it.

    Asserting the silence is deliberate, the same instrument as the wrong-
    sentence record: narrow the suppression scan later and these fail, forcing
    the paragraphs in `_VALUE_MEASUREMENT_RELAXED` to be corrected with the code.

    The rows are also a diagonal, one narrowing each, which is what keeps the
    #451 notation count honest and the bound's cost from being implied: shorten
    the magnitude run to a single word and `2천만주 보유, 3개월 준비` alone fires;
    drop `백` from `_SINO_KOREAN_MAGNITUDES` and `3백주 보유, 3개월 준비` alone
    fires; narrow the number's `\\d` back to `[0-9]` and
    `１００주 보유, 3개월 준비` alone fires; drop the `(?![가-힣A-Za-z])` from
    `_UNIT_SHADOW_GUARD` and `3분기실적, 2시간 소요` alone fires. Those four
    sentences are swept below rather than asserted here, which is the whole
    reason they can be written down: a sentence saying a test re-derives
    something is itself a claim about the test, and only the sweep makes it one.

    A docstring saying "two notations" would price every value except the ones
    the third reaches, and `３백주 보유, 3개월 준비` was declined as the digit row
    because it fires under two of these -- which is the result that would make
    the sweep below dirty, since a row with two causes fails its own row half.

    Read the list as an illustration and not as a set: every reading this scan
    still makes reaches into all three notations, so a count here would be a
    count of an open class.
    """
    import verinote.pipeline.query_measure_unit as qmu
    from verinote.pipeline.query_measure_unit import (
        _value_measure_units,
        korean_measure_unit_mismatch,
    )

    assert korean_measure_unit_mismatch(question, value) is None
    # The mismatch really is in the value; only the suppression scan hides it.
    assert unit_the_caveat_used_to_name in {
        spelling for _, spelling in _value_measure_units(value)
    }

    # The diagonal, re-derived. Each parametrization takes its own ROW of the
    # 4x4 -- this value fires under exactly one narrowing, so its cause is
    # single -- and its own COLUMN -- under that narrowing no other row fires,
    # which is the "alone" in each sentence above. Four parametrizations cover
    # the matrix and neither half implies the other.
    narrowings = _lost_caveat_narrowings()
    assert narrowing_that_fires_it in narrowings

    def fires(pattern, asked, text):
        monkeypatch.setattr(qmu, "_VALUE_MEASUREMENT_RELAXED", pattern)
        return korean_measure_unit_mismatch(asked, text) is not None

    assert [
        name for name, pattern in narrowings.items() if fires(pattern, question, value)
    ] == [narrowing_that_fires_it]
    assert [
        other
        for asked, other, _, _ in _LOST_CAVEAT_ROWS
        if other != value and fires(narrowings[narrowing_that_fires_it], asked, other)
    ] == []


_SHADOW_WORD_FIXTURES = {
    "분기": ("샘플사업의 소요는 몇 분인가?", "3분기 실적, 2시간 소요", ("분", "시간")),
    "주년": ("샘플사업의 기간은 몇 주인가?", "1주년 기념, 3개월 준비", ("주", "개월")),
    "년대": ("샘플사업의 기간은 몇 년인가?", "80년대 후반, 3개월", ("년", "개월")),
    "주주": ("샘플사업의 기간은 몇 주인가?", "3천만 주주, 3개월", ("주", "개월")),
    "secondary": (
        "샘플사업의 소요는 몇 초인가?",
        "3 secondary reviews, 2 minutes",
        ("초", "minutes"),
    ),
}
"""One caveat per member of `_UNIT_SHADOW_WORDS`, with the pair it must name.

A literal dict plus a set-equality assertion, not a parametrize over the live
tuple: parametrizing would delete the case along with the member and the test
would pass vacuously. Same shape as `_MONTH_WORD_FIXTURES`, for the same reason.

The last two carry whitespace between the number and the spelling, which is what
the number's trailing `\\s*` has to cross before the guard is consulted.
"""

_SHADOW_WORD_NOTATION_TWINS = [
    ("샘플사업의 소요는 몇 분인가?", "２분기 실적, 2시간 소요", ("분", "시간")),
    ("샘플사업의 기간은 몇 주인가?", "１주년 기념, 3개월 준비", ("주", "개월")),
    ("샘플사업의 기간은 몇 년인가?", "８０년대 후반, 3개월", ("년", "개월")),
]
"""The full-width twins of the first three fixtures.

`_VALUE_MEASUREMENT_RELAXED` claims the guard travels into the notations #451
widened the number into, because the guard is placed on the spelling and not on
the number. These are that claim, re-derived. Without them it would be a
sentence about the guard's reach that nothing measures.
"""


def test_a_word_a_unit_spelling_only_begins_is_not_that_unit():
    """#453: a spelling standing at the head of a listed word is not that unit.

    Each of these named its neighbour before the suppression scan existed, went
    silent once that scan read the spelling standing at the head of a longer
    word, and names it again. The pairs are exact rather than "not None", so a
    row cannot pass by the caveat naming some other unit.

    Two failing modes, both run, and the loop comes first so that they stay
    distinct. Delete a member from `_UNIT_SHADOW_WORDS` and exactly the rows that
    member reads go silent -- its own, and for the first three the full-width
    twin of it -- while no OTHER member's row moves; the diagonal was measured,
    not assumed, so no member is being held up by another. Add a member and the
    set equality is what fires, naming the fixture nobody wrote.
    """
    from verinote.pipeline.query_measure_unit import (
        _UNIT_SHADOW_WORDS,
        korean_measure_unit_mismatch,
    )

    for member, (question, value, expected) in _SHADOW_WORD_FIXTURES.items():
        assert korean_measure_unit_mismatch(question, value) == expected, member
    for question, value, expected in _SHADOW_WORD_NOTATION_TWINS:
        assert korean_measure_unit_mismatch(question, value) == expected, value
    assert set(_SHADOW_WORD_FIXTURES) == set(_UNIT_SHADOW_WORDS)


def test_a_shadow_word_is_one_the_reporting_scan_already_refuses():
    """The tripwire on `_UNIT_SHADOW_WORDS`, which is an open lexical class.

    The criterion a member has to meet is not a judgement about Korean this test
    could make. It is that the REPORTING scan already reads the member's complete
    form as no quantity at all, so listing it puts the two scans on the same
    reading of that value rather than on different ones.

    The disaster it refuses: `분간` is a real suffix form, `_value_measure_units`
    reads `3분간` as MINUTE, and with the `일간` shape of it listed
    `korean_measure_unit_mismatch("...몇 일인가?", "3일간")` returns `('일', '일')`
    -- a caveat telling the reader the value states the unit the question asked
    in. Measured, not reasoned: adding any of `일간`, `분간`, `년간`, `주간` or
    `초간` reddens the loop below.

    What follows the loop is the premise `_VALUE_MEASUREMENT_RELAXED`'s
    backtracking argument rests on. The number's trailing `\\s*` and magnitude
    run are greedy and backtrackable, so a match could in principle be retried
    one character earlier and dodge a guard placed after the number -- it cannot,
    because no spelling begins with any character the number itself consumes, so
    there is no earlier start at which the unit group still matches. That is all
    four classes and not three: `[\\d,.\\s]` plus `_SINO_KOREAN_MAGNITUDES`, and
    the separators belong there because `1,000년` and `1.5주년` retry inside the
    digit run rather than before it. Add a spelling opening with any of them and
    this fails here rather than silently. `180년대`, `1980년대`,
    `1.5주년`, `2백주주`, `3  분기` and `3\\xa0secondary` are the shapes that
    argument is about, and they read no unit.
    """
    from verinote.pipeline.query_measure_unit import (
        _MEASUREMENT_UNIT_SPELLINGS,
        _SINO_KOREAN_MAGNITUDES,
        _UNIT_SHADOW_WORDS,
        _VALUE_MEASUREMENT_RELAXED,
        _value_measure_units,
    )

    assert _UNIT_SHADOW_WORDS, "an empty tuple would make the loop vacuous"
    for member in _UNIT_SHADOW_WORDS:
        assert _value_measure_units(f"3{member}") == (), member

    assert [
        s
        for s in _MEASUREMENT_UNIT_SPELLINGS
        if s[0].isdigit()
        or s[0].isspace()
        or s[0] in ",."
        or s[0] in _SINO_KOREAN_MAGNITUDES
    ] == []
    for value in ("180년대", "1980년대", "1.5주년", "2백주주", "3  분기", "3\xa0secondary"):
        assert [m.group("unit") for m in _VALUE_MEASUREMENT_RELAXED.finditer(value)] == []


def _relaxed_pattern_with_guard(guard):
    """The shipped relaxed pattern with its GUARD replaced, for the mutants.

    The twin of `_relaxed_pattern_from`, which replaces the number instead. Each
    holds every part it is not replacing live, and
    `test_the_pattern_rebuilds_are_still_the_shipped_pattern` checks both against
    the shipped pattern with the replaced part put back.
    """
    import re

    from verinote.pipeline.query_measure_unit import (
        _MEASUREMENT_UNIT_SPELLINGS,
        _RELAXED_QUANTITY_NUMBER,
    )

    return re.compile(
        _RELAXED_QUANTITY_NUMBER
        + guard
        + r"(?P<unit>"
        + "|".join(
            re.escape(s) for s in sorted(_MEASUREMENT_UNIT_SPELLINGS, key=len, reverse=True)
        )
        + r")"
    )


def test_the_pattern_rebuilds_are_still_the_shipped_pattern():
    """Both mutant factories reproduce `_VALUE_MEASUREMENT_RELAXED` exactly.

    A mutant is only evidence about the part it replaces if everything else in it
    is the shipped thing. Both helpers read the other parts live, which stops
    them DRIFTING, but reading live does not stop a part being dropped outright:
    delete `_UNIT_SHADOW_GUARD` from either factory and every mutant built from
    it differs from shipped in two places, while nothing else compares a rebuild
    to the original. Measured, on this suite: with the guard deleted from
    `_relaxed_pattern_from` and this test absent, everything else passes.

    Put the replaced part back and the rebuild must be the shipped pattern,
    character for character. Asserted on `.pattern` and `.flags` rather than on
    readings, and the measurement above is the whole argument for that: no corpus
    any caller already runs discriminates the two, or deleting the guard would
    have reddened something.

    The two are not interchangeable, and the direction matters. Readings-equal
    does NOT imply characters-equal: `reversed-table` and `reverse-alphabetical`
    both keep `달러` before `달` and read identically to the shipped ordering, on
    the probes in `test_only_the_달러_before_달_constraint_decides_the_suppression_ordering`
    and on a wider sweep, while their pattern text differs. Characters-equal plus
    flags-equal is the same compiled object, so it implies every reading and
    cannot go blind the way a corpus check can -- which is what a rebuild site
    needs, since what it must guarantee is what the rebuild IS and not what some
    corpus happens to see.

    This lives outside the helpers rather than inside them because it has to read
    `_VALUE_MEASUREMENT_RELAXED`, and a caller can be holding that monkeypatched
    to a mutant at the moment it calls -- the second `monkeypatch.setattr` in
    `test_the_shadow_guard_gains_no_caveat_on_a_word_it_only_prefixes` evaluates
    its argument while the first patch is live, so an in-helper assert compares
    the rebuild against a mutant and reddens a passing test. One such caller is
    enough, and that one was found by instrumenting the helpers rather than by
    reading them.
    """
    from verinote.pipeline.query_measure_unit import (
        _RELAXED_QUANTITY_NUMBER,
        _UNIT_SHADOW_GUARD,
        _VALUE_MEASUREMENT_RELAXED,
    )

    shipped = (_VALUE_MEASUREMENT_RELAXED.pattern, _VALUE_MEASUREMENT_RELAXED.flags)
    for rebuilt in (
        _relaxed_pattern_from(_RELAXED_QUANTITY_NUMBER),
        _relaxed_pattern_with_guard(_UNIT_SHADOW_GUARD),
    ):
        assert (rebuilt.pattern, rebuilt.flags) == shipped


def _unbounded_shadow_guard():
    """`_UNIT_SHADOW_GUARD` with its right bound removed, derived from the live one.

    This is the mechanism #453 rejected: it refuses a listed word wherever the
    word merely BEGINS, which is also wherever a longer STRING beginning with it
    does. Derived rather than retyped so that changing the bound in the module
    changes the mutant with it instead of leaving a stale copy to compare against.
    """
    from verinote.pipeline.query_measure_unit import _UNIT_SHADOW_GUARD

    unbounded = _UNIT_SHADOW_GUARD.replace(r"(?![가-힣A-Za-z])", "")
    assert unbounded != _UNIT_SHADOW_GUARD, "the mutation did not apply"
    return unbounded


def test_the_shadow_guard_does_not_narrow_a_quantity_it_only_prefixes(monkeypatch):
    """The two classes the guard must leave suppressed, and one control.

    The first group is what a right-hand boundary on the UNIT would have cost,
    and the class that decides it is josa: Korean orthography writes a particle
    flush against the noun, so `3주의 준비, 2개월 소요` states three weeks with a
    particle attached and every boundary shape weighed for this scan caveats it
    against a value that does state the asked unit. Two rows are not that class
    and are here for their own reasons. `3일간의 일정, 2주 소요` survives one of
    the three shapes -- a `_UNIT_SUFFIX` member stands between the unit and the
    Hangul, so it is not the deciding case the file once took it for -- and
    `3시간30분` survives all three, being the value the trailing lookahead was
    dropped for in the first place rather than a boundary cost.

    That group is a regression guard against the rejected alternatives coming
    back rather than a pin on the shipped mechanism, and it does not redden under
    any single mutation of the guard that was run for #453 -- not dropping the
    bound, not deleting the guard, not harmonising the bound, not adding or
    removing a member. It is not inert, though, and the combination that moves it
    is worth recording, because it shows the two halves of the guard hold it up
    together: an UNBOUNDED guard with `일` listed caveats `3일은 걸린다, 2주 소요`,
    `3일째 진행, 2주 소요` and `3일간의 일정, 2주 소요`. The bound rules out one
    half of that and
    `test_a_shadow_word_is_one_the_reporting_scan_already_refuses` rules out the
    other.

    The second group is what the guard's right bound pins, and it does have a
    live failing mode. Each has a listed word standing exactly where the spelling
    does, continued flush by another Hangul letter, and each really does state
    the asked unit -- thirty minutes taken as a basis, a ten-year loan, a
    two-week cycle. Drop `(?![가-힣A-Za-z])` from `_UNIT_SHADOW_GUARD` and every
    one of them is caveated. That is asserted below rather than described, so the
    group cannot go inert without saying so.

    `30분간격, 2시간 소요` is the control, and it is here because a corpus in
    which even it regressed would be measuring something other than the guard:
    `간격` does not begin with `기`, so no build of this guard can bite it. It
    stays `None` on both sides.
    """
    import verinote.pipeline.query_measure_unit as qmu
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    boundary_alternative_cost = [
        ("샘플사업의 기간은 몇 주인가?", "3주의 준비, 2개월 소요"),
        ("샘플사업의 기간은 몇 일인가?", "3일은 걸린다, 2주 소요"),
        ("샘플사업의 기간은 몇 개월인가?", "3개월로 연장, 2주 소요"),
        ("샘플사업의 소요는 몇 시간인가?", "3시간동안 진행, 2주 소요"),
        ("샘플사업의 기간은 몇 일인가?", "3일째 진행, 2주 소요"),
        ("샘플사업의 기간은 몇 년인가?", "3년이상 근무, 2개월"),
        ("샘플사업의 기간은 몇 일인가?", "3일간의 일정, 2주 소요"),
        ("샘플작업의 소요시간은 몇 시간인가?", "3시간30분"),
    ]
    continued_flush = [
        ("샘플사업의 소요는 몇 분인가?", "30분기준 회의, 2시간 소요"),
        ("샘플사업의 소요는 몇 분인가?", "30분기록, 2시간 소요"),
        ("샘플사업의 기간은 몇 년인가?", "10년대출 상환, 3개월 준비"),
        ("샘플사업의 기간은 몇 년인가?", "3년대비 증가, 2개월"),
        ("샘플사업의 기간은 몇 주인가?", "2주주기로 반복, 3개월"),
        ("샘플사업의 기간은 몇 주인가?", "3주주말 근무, 2개월"),
    ]
    control = ("샘플사업의 소요는 몇 분인가?", "30분간격, 2시간 소요")

    for question, value in boundary_alternative_cost + continued_flush + [control]:
        assert korean_measure_unit_mismatch(question, value) is None, value

    # The bound is load-bearing, re-derived: without it the second group is a
    # wrong sentence apiece and the control is still silent.
    monkeypatch.setattr(
        qmu,
        "_VALUE_MEASUREMENT_RELAXED",
        _relaxed_pattern_with_guard(_unbounded_shadow_guard()),
    )
    for question, value in continued_flush:
        assert korean_measure_unit_mismatch(question, value) is not None, value
    assert korean_measure_unit_mismatch(*control) is None


def _shadow_grid():
    """Values whose tail both continues a unit spelling and carries a neighbour.

    Four kinds, and the point of the partition is that the guard is required to
    behave differently in each. A tail that only continues a spelling changes no
    ANSWER, because there is no same-family unit left for a caveat to name; a
    tail that only carries a neighbour never reaches the guard. A grid built from
    the product of those two tail sets measures nothing, which is how the first
    sweep written for #453 came back with ten rows that were all one English
    shape. Every row here is both at once.

    `개월` is the neighbour throughout, and the assertion beside it re-derives
    the property that matters: it is never the unit the row asks in, so every row
    has a caveat available to gain or lose. That it is in the same family as each
    of them is not asserted here -- it is what the `SHADOW-SPACED` gain measures,
    and a neighbour outside the family would show up as that cell going empty.

    The continuing letters are not all Korean words, and they do not need to be.
    What the bound keys on is whether a Hangul or Latin letter stands after the
    listed word, so the letter is the property under test and the lexicon is not.
    The real words are witnesses in
    `test_the_shadow_guard_does_not_narrow_a_quantity_it_only_prefixes`.
    """
    import re

    from verinote.pipeline.query_measure_unit import (
        _MEASUREMENT_UNIT_SPELLINGS,
        _UNIT_SHADOW_WORDS,
    )

    neighbour = "개월"
    rows = []
    for member in _UNIT_SHADOW_WORDS:
        latin = member.isascii()
        for spelling in _MEASUREMENT_UNIT_SPELLINGS:
            if spelling == member or not member.startswith(spelling):
                continue
            unit = _MEASUREMENT_UNIT_SPELLINGS[spelling]
            assert _MEASUREMENT_UNIT_SPELLINGS[neighbour] != unit, spelling
            # The counter has to be Korean: `_question_measure_unit` returns
            # None for `몇 second인가?`, so a Latin one would make every row of
            # that member silent under every build and the cell inert.
            counter = next(
                k
                for k, v in _MEASUREMENT_UNIT_SPELLINGS.items()
                if v == unit and re.fullmatch(r"[가-힣]+", k)
            )
            head = member[len(spelling) :]
            noun = "work" if latin else "실적"
            plains = ("", f" {noun}") + (
                ("s ready",) if latin else ("의 준비", "간 진행", "째 진행")
            )
            continuations = ("ies", "x") if latin else ("준", "록", "출", "비", "말")
            for number in ("3", "80", "2천만", "１００"):
                stem = f"{number} {spelling}" if latin else f"{number}{spelling}"
                tails = (
                    [("PLAIN", plain) for plain in plains]
                    + [("SHADOW-SPACED", f"{head} {noun}")]
                    + [("SHADOW-FLUSH", f"{head}{noun}")]
                    + [("CONTINUED", f"{head}{c} {noun}") for c in continuations]
                )
                for kind, tail in tails:
                    rows.append((kind, counter, f"{stem}{tail}, 2{neighbour} 소요"))
    return rows


def test_the_shadow_guard_gains_no_caveat_on_a_word_it_only_prefixes(monkeypatch):
    """The load-bearing cell is `CONTINUED`, and it must be empty.

    A caveat GAINED is not by itself evidence of anything here: this guard can
    only remove readings from `_value_states_asked_unit`, whose one effect is an
    early `None`, so it can only ever add caveats and no corpus can show it
    losing one. "Nothing was lost" is therefore a theorem about the instrument
    and is deliberately not asserted. What decides whether the change is right is
    WHICH cell gained -- a gain in `CONTINUED` is a caveat fired against a value
    that does state the asked unit, which is a wrong sentence in front of a
    reader.

    Three builds, so that every cell is a comparison and not a reading: the
    pattern as shipped, no guard at all (what the scan did before #453), and the
    unbounded guard #453 rejected. The shipped side is swept off the module
    attribute before anything is patched, and not off a rebuild, so detaching the
    guard from `_VALUE_MEASUREMENT_RELAXED` reddens this too rather than leaving
    it measuring a constant nothing uses.

    Failing modes, both run: replacing the shipped guard with the unbounded one
    fills `CONTINUED` -- which is also this test's non-vacuity check, since a
    cell that could not have come back dirty is not a check -- and removing the
    guard from the shipped pattern empties `SHADOW-SPACED`. That gain is
    re-derived and compared against zero rather than written as a number, because
    a number in a test rots as quietly as one in a docstring.
    """
    import verinote.pipeline.query_measure_unit as qmu
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    grid = _shadow_grid()
    assert grid, "the grid must not be empty"

    def sweep():
        return [
            korean_measure_unit_mismatch(f"샘플사업의 기간은 몇 {counter}인가?", value)
            for _, counter, value in grid
        ]

    shipped = sweep()
    monkeypatch.setattr(
        qmu, "_VALUE_MEASUREMENT_RELAXED", _relaxed_pattern_with_guard("")
    )
    before = sweep()
    monkeypatch.setattr(
        qmu,
        "_VALUE_MEASUREMENT_RELAXED",
        _relaxed_pattern_with_guard(_unbounded_shadow_guard()),
    )
    unbounded = sweep()

    def changed(kind, after):
        return [
            (value, b, a)
            for (row_kind, _, value), b, a in zip(grid, before, after)
            if row_kind == kind and b != a
        ]

    # The assertion the change stands or falls on.
    assert changed("CONTINUED", shipped) == []
    # ... and the proof this cell can come back dirty at all.
    assert changed("CONTINUED", unbounded) != []

    # A tail that reaches no listed word is untouched by any build.
    assert changed("PLAIN", shipped) == []
    assert changed("PLAIN", unbounded) == []

    # What the change buys, re-derived rather than quoted.
    spaced = changed("SHADOW-SPACED", shipped)
    assert len(spaced) > 0
    assert all(b is None and a is not None for _, b, a in spaced)

    # What the bound gives up, recorded here as it is in the record table: a
    # listed word continued flush stays read, so its caveat stays lost.
    assert changed("SHADOW-FLUSH", shipped) == []
    assert changed("SHADOW-FLUSH", unbounded) != []


def test_the_shadow_bound_admits_a_digit_and_refuses_a_letter(monkeypatch):
    """Why `_UNIT_SHADOW_GUARD`'s bound is not `_VALUE_MEASUREMENT`'s lookahead.

    The two character classes differ by `0-9` and harmonising them is the obvious
    later tidy-up. It is wrong here, and the reason is positional rather than
    stylistic: `_VALUE_MEASUREMENT`'s lookahead stands AFTER a unit it has read,
    where a following digit means the unit ran into another number and the
    reading is doubtful. This bound stands after a word the guard is deciding
    whether to REFUSE, where a following digit means that word ended and a new
    number began -- so the word did stand complete and the refusal is right.

    The three values below each state two decades, quarters or anniversaries and
    no duration in the asked unit, so the caveat naming their `개월` or `시간` is
    the true answer. Admitting `0-9` into the bound loses all three, which is
    asserted rather than described.

    The result that would have made this check dirty is the two classes reading
    these values alike: the harmonised build would then still caveat them and the
    second loop would fail, which is what says the difference is real rather than
    a distinction on paper. `3분기실적, 2시간 소요` is the other direction on the
    same bound -- a Hangul letter after the listed word, where both classes agree
    the guard must not bite -- so it is silent on both sides and is the row that
    would catch a mutation flipping the bound's sense rather than its class.
    """
    import verinote.pipeline.query_measure_unit as qmu
    from verinote.pipeline.query_measure_unit import (
        _UNIT_SHADOW_GUARD,
        korean_measure_unit_mismatch,
    )

    digit_continued = [
        ("샘플사업의 기간은 몇 년인가?", "80년대2000년대 비교, 3개월", ("년", "개월")),
        ("샘플사업의 소요는 몇 분인가?", "3분기4분기 실적, 2시간 소요", ("분", "시간")),
        ("샘플사업의 기간은 몇 주인가?", "1주년2주년 기념, 3개월", ("주", "개월")),
    ]
    letter_continued = ("샘플사업의 소요는 몇 분인가?", "3분기실적, 2시간 소요")

    for question, value, expected in digit_continued:
        assert korean_measure_unit_mismatch(question, value) == expected, value
    assert korean_measure_unit_mismatch(*letter_continued) is None

    harmonised = _UNIT_SHADOW_GUARD.replace("[가-힣A-Za-z]", "[가-힣0-9A-Za-z]")
    assert harmonised != _UNIT_SHADOW_GUARD, "the mutation did not apply"
    monkeypatch.setattr(
        qmu, "_VALUE_MEASUREMENT_RELAXED", _relaxed_pattern_with_guard(harmonised)
    )
    for question, value, _ in digit_continued:
        assert korean_measure_unit_mismatch(question, value) is None, value
    assert korean_measure_unit_mismatch(*letter_continued) is None


def test_a_point_in_time_silence_travels_into_the_new_notations():
    """#451's second cost, which is not the lost-caveat class above it.

    `_value_states_asked_unit` does not consult `_TIME_POINT` -- that pattern's
    own docstring says so -- so a point-in-time component the suppression scan
    reads as a quantity suppresses in whatever notation it is written. Widening
    the number therefore carried every point-in-time silence into the new
    notations too, and filing that under the lost-caveat class would be a false
    claim about the code: nothing here is a unit spelling hiding inside another
    word.

    Each row is asserted beside its ASCII twin, which is silent as well. That
    pairing is what makes this an extension of an accepted silence rather than a
    new misreading -- and it is also the falsifier, since a row whose twin was
    caveated would belong somewhere else. These are real losses judged as
    Korean: `２０２１년 착수, 총 3주` asked in years carries no duration in years
    and the caveat naming the weeks was true.

    One reading went the other way with them, and it is the defect this scan
    exists to fix: `３시간30분` asked in hours was told it states minutes.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    for question, wide, ascii_twin in [
        ("샘플사업의 기간은 몇 년인가?", "２０２１년 착수, 총 3주", "2021년 착수, 총 3주"),
        ("샘플계약의 마감일은 몇 일인가?", "１５일 마감, 3주", "15일 마감, 3주"),
        ("샘플계약의 마감일은 몇 일인가?", "３월 １５일, 3주", "3월 15일, 3주"),
        ("샘플사업의 소요는 몇 분인가?", "３시 ３０분 회의, 2시간", "3시 30분 회의, 2시간"),
        ("샘플사업의 기간은 몇 년인가?", "２１년, 3개월", "21년, 3개월"),
    ]:
        assert korean_measure_unit_mismatch(question, wide) is None, wide
        assert korean_measure_unit_mismatch(question, ascii_twin) is None, ascii_twin

    assert korean_measure_unit_mismatch("샘플작업의 소요시간은 몇 시간인가?", "３시간30분") is None


def test_only_the_달러_before_달_constraint_decides_the_suppression_ordering():
    """The ordering requirement is one prefix pair, not the length rule.

    `달`/`달러` is the only prefix pair in the table that crosses canonical
    units, so it is the only one whose order can change an answer. Longest-first
    is what ships because it states the intent, but it is not privileged.

    Pinned in both directions, and derived from the table rather than hardcoded
    so that reordering `_MEASUREMENT_UNIT_SPELLINGS` cannot make this fail for a
    reason it is not about: every candidate ordering that puts `달러` first reads
    exactly as the shipped pattern does, and every one that does not reads
    differently. The two non-empty assertions keep it from passing vacuously if
    some future table put every candidate on one side.

    The candidates are rebuilt from the shipped pattern's named parts rather than
    copied, and each rebuild below drops one of them and asserts the probes
    notice. That is not tidiness: this test carried a COPY of the number until
    #451 changed the number under it and nothing here noticed, and when #453
    added `_UNIT_SHADOW_GUARD` not one of the ten probes could tell a guarded
    rebuild from a guard-free one, so the comparison had quietly stopped being
    about the shipped pattern again. The guard has two parts that can go blind
    separately -- the word list and its right bound -- and `30분기준` is in the
    probe set because it is the only one of these that sees the bound.
    """
    import re

    from verinote.pipeline.query_measure_unit import (
        _MEASUREMENT_UNIT_SPELLINGS,
        _RELAXED_QUANTITY_NUMBER,
        _UNIT_SHADOW_GUARD,
        _VALUE_MEASUREMENT_RELAXED,
    )

    spellings = list(_MEASUREMENT_UNIT_SPELLINGS)
    candidates = {
        "longest-first": sorted(spellings, key=len, reverse=True),
        "shortest-first": sorted(spellings, key=len),
        "table": list(spellings),
        "reversed-table": list(reversed(spellings)),
        "alphabetical": sorted(spellings),
        "reverse-alphabetical": sorted(spellings, reverse=True),
    }
    # The last two probes are the range the number gained in #451. Without them
    # every probe is ASCII with at most one magnitude word, so the copy of the
    # number this test used to carry could not be told apart from the live one
    # and substituting the constant would stop the copy drifting while leaving
    # the probes blind at the level above.
    # `3분기 실적` through `30분기준` are the range the guard gained in #453, and
    # the last of the three is the only one of them that tells the shipped
    # bounded guard from the unbounded one it was chosen over.
    probes = [
        "3달러", "2주일", "3만 원", "1000달러", "3달", "2주", "30 seconds", "5달러 및 3달",
        "2천만달러 및 3달", "３달러", "3분기 실적", "3천만 주주", "30분기준",
    ]

    def reads(pattern):
        return [[m.group("unit") for m in pattern.finditer(v)] for v in probes]

    def compiled(order):
        return re.compile(
            _RELAXED_QUANTITY_NUMBER
            + _UNIT_SHADOW_GUARD
            + r"(?P<unit>"
            + "|".join(re.escape(s) for s in order)
            + r")"
        )

    shipped = reads(_VALUE_MEASUREMENT_RELAXED)
    # The probes really do exercise the number, and not only the alternation:
    # the pre-#451 number reads them differently. The guard is held constant so
    # that the difference is the number and nothing else.
    before_451 = re.compile(
        r"[0-9][0-9,.]*\s*[만억천조]?\s*"
        + _UNIT_SHADOW_GUARD
        + r"(?P<unit>"
        + "|".join(re.escape(s) for s in candidates["longest-first"])
        + r")"
    )
    assert reads(before_451) != shipped, "the probes cannot see the number"
    # And they exercise the guard, with the number held constant in turn -- both
    # halves of it, since a probe set that saw the word list but not the bound
    # would be back where the ten probes were before #453.
    before_453 = re.compile(
        _RELAXED_QUANTITY_NUMBER
        + r"(?P<unit>"
        + "|".join(re.escape(s) for s in candidates["longest-first"])
        + r")"
    )
    assert reads(before_453) != shipped, "the probes cannot see the shadow guard"
    unbounded = re.compile(
        _RELAXED_QUANTITY_NUMBER
        + _unbounded_shadow_guard()
        + r"(?P<unit>"
        + "|".join(re.escape(s) for s in candidates["longest-first"])
        + r")"
    )
    assert reads(unbounded) != shipped, "the probes cannot see the guard's bound"

    satisfying = {n: o for n, o in candidates.items() if o.index("달러") < o.index("달")}
    violating = {n: o for n, o in candidates.items() if o.index("달러") > o.index("달")}
    assert satisfying, candidates
    assert violating, candidates
    for name, order in satisfying.items():
        assert reads(compiled(order)) == shipped, name
    for name, order in violating.items():
        assert reads(compiled(order)) != shipped, name


@pytest.mark.parametrize(
    "value",
    ["12.5.3 버전, 3주 소요", "10.1.2 릴리스, 3주", "10.0.0.1 서버, 3주"],
)
def test_a_version_triple_no_longer_swallows_the_duration_beside_it(value):
    """The third cost of the ISO widening, taken back by #452.

    Widening the ISO year to two digits made dotted numeric triples look like
    dates, so a version number beside a genuine duration silenced it. Each of
    these was caveated `(개월, 주)` before the widening, lost that under the
    whole-value guard, and has it back under the span-local one: the triple
    still matches, and a version number states no unit of its own, so a duration
    standing clear of it is reported. Written flush onto the end of the triple it
    is not: `12.5.33주` and `10.1.23주` have their digit run begin at the
    version's own first digit, so the whole quantity overlaps the span and is
    dropped. That is the residue the ISO branch's own paragraph records, reached
    by the same mechanism, and not a second one.

    `1.2.3 버전, 3주` now gives the same answer as the other three, so it no
    longer constrains the triple's `[0-9]{2,4}` lower bound. The value that
    still does is `1.2.3 일`: it states days where `12.5.3 일` does not, and
    widening the first component to one digit makes it silent. That is the only
    fixture holding the bound, which is why it is asserted here rather than left
    to the sentence in `_TIME_POINT` that describes it.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    question = "샘플사업의 기간은 몇 개월인가?"
    assert korean_measure_unit_mismatch(question, value) == ("개월", "주")
    assert korean_measure_unit_mismatch(question, "1.2.3 버전, 3주") == ("개월", "주")
    # What the lower bound still separates, re-derived rather than described.
    assert korean_measure_unit_mismatch(question, "1.2.3 일") == ("개월", "일")
    assert korean_measure_unit_mismatch(question, "12.5.3 일") is None


def test_달러_is_the_only_prefix_pair_that_crosses_canonical_units():
    """The premise the suppression ordering rests on, derived from the table.

    `_VALUE_MEASUREMENT_RELAXED` argues that dropping the lookahead is safe
    because sorting settles the one prefix pair whose order can change an
    answer. That argument is only as good as the premise, and nothing checked
    it: the ordering test's probes are chosen values, so a row introducing a
    second cross-unit prefix pair would falsify the paragraph and leave the
    suite green.

    Both counts are computed here rather than quoted, for the same reason the
    digit-prefix figure is.
    """
    from verinote.pipeline.query_measure_unit import _MEASUREMENT_UNIT_SPELLINGS as spellings

    prefix_pairs = [
        (short, long)
        for short in spellings
        for long in spellings
        if short != long and long.startswith(short)
    ]
    crossing = [
        (short, long)
        for short, long in prefix_pairs
        if spellings[short] != spellings[long]
    ]
    assert len(prefix_pairs) == 10
    assert crossing == [("달", "달러")]


def test_a_unit_suffix_would_be_inert_here():
    """Why `_UNIT_SUFFIX` is absent from the relaxed pattern, re-derived.

    Not the general claim that a match's end is unread -- it is read, `finditer`
    resumes from it, and the two-line counterexample below shows an optional
    trailing group changing what a scan finds. The reason is specific to these
    character sets: every suffix member is Hangul and every match starts at a
    digit, so a moved end can never swallow a later match's start.

    Both halves are asserted, because only the pair is the argument: ends really
    do move, and readings really do not.

    The last loop is the one that has to be kept honest, and it went blind once
    already: it claims the shipped pattern is the suffix-free rebuild, and when
    #453 added `_UNIT_SHADOW_GUARD` to the shipped pattern not one of the 6400
    corpus values could tell the two apart, so the guard could have been deleted
    with this test green. The tails carrying a listed `_UNIT_SHADOW_WORDS` member
    are what fixes that, and the discrimination assertion beside the rebuild is
    what will say so the next time a part is added and the corpus does not
    follow.
    """
    import re

    from verinote.pipeline.query_measure_unit import (
        _MEASUREMENT_UNIT_SPELLINGS,
        _RELAXED_QUANTITY_NUMBER,
        _UNIT_SHADOW_GUARD,
        _UNIT_SUFFIX,
        _UNIT_SUFFIX_MEMBERS,
        _VALUE_MEASUREMENT_RELAXED,
    )

    # The general rule this reason is NOT: an optional trailing group does
    # change later matches when its characters can begin one.
    assert [m.group("u") for m in re.finditer(r"(?P<u>[ab])", "ab")] == ["a", "b"]
    assert [m.group("u") for m in re.finditer(r"(?P<u>[ab])(?:b)?", "ab")] == ["a"]

    # The premise, over the LIVE members rather than a copy of them. A literal
    # here would keep passing if a non-Hangul member were added, which is the
    # one change that breaks the argument: a digit member makes `3년2주` read
    # `년` alone, and `몇 주인가?` against it answers "the verified value states
    # 주". The corpus below carries `<quantity><quantity>` shapes so that the
    # readings loop sees it too, not only this assertion.
    assert _UNIT_SUFFIX_MEMBERS, "an empty member set would make this vacuous"
    for member in _UNIT_SUFFIX_MEMBERS:
        assert re.fullmatch(r"[가-힣]+", member), member

    # Both patterns are built from parts rather than by appending to the shipped
    # one. Deriving `with_suffix` from `_VALUE_MEASUREMENT_RELAXED.pattern` made
    # this test fail if the suffix were ever put back -- it would then be
    # comparing one suffix against two, the ends would stop moving, and the
    # non-vacuity guard below would fire on a mutation that breaks nothing. The
    # claim is about these character sets, not about today's pattern text.
    unit_group = (
        r"(?P<unit>"
        + "|".join(
            re.escape(s) for s in sorted(_MEASUREMENT_UNIT_SPELLINGS, key=len, reverse=True)
        )
        + r")"
    )
    quantity = _RELAXED_QUANTITY_NUMBER + _UNIT_SHADOW_GUARD + unit_group
    without_suffix = re.compile(quantity)
    with_suffix = re.compile(quantity + _UNIT_SUFFIX)
    # The rebuild that omits the guard, so the corpus can be asked whether it
    # sees one at all.
    without_guard = re.compile(_RELAXED_QUANTITY_NUMBER + unit_group)
    corpus = [
        f"{number}{magnitude}{spelling}{tail}"
        # The last three numbers are the three notations #451 added: a
        # magnitude run, a magnitude outside `[만억천조]`, and a non-ASCII
        # digit. Without them the corpus stays inside the pre-#451 number and
        # this test cannot see the pattern it is built from.
        for number in ("3", "1000", "2천만", "2백", "３")
        for magnitude in ("", "만")
        for spelling in _MEASUREMENT_UNIT_SPELLINGS
        # `2주` through `5초` are the suffix's distinguishing shape: something
        # follows the suffix position, so a member that could begin a match
        # would swallow it. The rest, from `기` on, complete or continue a
        # member of `_UNIT_SHADOW_WORDS` -- flush, spaced and continued in turn
        # -- which is the guard's distinguishing shape and #453 added them.
        for tail in (
            "", "간", "가량", "정도", "쯤", "짜리", "차", "의", " ", "5", "s", "러",
            "2주", "간5주", "가량10초", "5초",
            "기", "기 ", "기준", "년", "년 ", "년대비", "대", "대 ", "대출",
            "주", "주 ", "주기", "ary", "ary ", "aries",
        )
    ]
    moved = sum(
        1 for value in corpus
        if [m.end() for m in with_suffix.finditer(value)]
        != [m.end() for m in without_suffix.finditer(value)]
    )
    assert moved > 0, "the suffix must actually match somewhere, or this is vacuous"
    for value in corpus:
        assert [m.group("unit") for m in with_suffix.finditer(value)] == [
            m.group("unit") for m in without_suffix.finditer(value)
        ], value

    # The corpus can tell the shipped pattern from a rebuild missing one of its
    # parts, which is what the equality below is worth anything for. Written
    # against the guard because that is the part the corpus was blind to.
    assert [
        value
        for value in corpus
        if [m.group("unit") for m in without_guard.finditer(value)]
        != [m.group("unit") for m in without_suffix.finditer(value)]
    ], "the corpus cannot see the shadow guard, so the equality below is vacuous"

    # And the shipped pattern is the suffix-free one, which is what the comment
    # beside it claims. Asserted on readings, not on pattern text, so restoring
    # the suffix -- inert, per the loop above -- would not fail this.
    for value in corpus:
        assert [m.group("unit") for m in _VALUE_MEASUREMENT_RELAXED.finditer(value)] == [
            m.group("unit") for m in without_suffix.finditer(value)
        ], value


# --- the point-in-time recogniser (#450) ------------------------------------

_MONTH = "샘플계약의 기간은 몇 개월인가?"
_HOURS = "샘플회의의 시간은 몇 시간인가?"

_MONTH_WORD_FIXTURES = {
    "매월": "매월 15일",
    "매달": "매달 1일",
    "금월": "금월 15일",
    "익월": "익월 15일",
    "내월": "내월 15일",
    "당월": "당월 15일",
    "전월": "전월 15일",
    "차월": "차월 15일",
    "다음 달": "다음 달 1일",
    "이번 달": "이번 달 1일",
    "지난 달": "지난 달 1일",
    "내달": "내달 15일",
}
"""One dated value per member of `_MONTH_WORD_MEMBERS`.

A literal dict plus a set-equality assertion, not a parametrize over the live
tuple: parametrizing would delete the case along with the member and the test
would pass vacuously. Same shape as `_ROW_FIXTURES`, for the same reason.
"""

_MONTH_PART_FIXTURES = {
    "의": "3월의 15일",
    "중": "3월 중 15일",
    "초": "3월 초 5일",
    "말": "3월 말 15일",
}
"""One dated value per member of `_MONTH_PART_MEMBERS`; see above for the shape."""


def test_every_month_word_has_a_day_it_reads_as_a_date():
    """Each month word makes the day beside it a date, and none rides free.

    Dropping any one member leaves its value stating `일`, so a question asked
    in `몇 개월` is told the verified value states `일` -- reading `매월 15일`
    as fifteen days rather than as the fifteenth. The set-equality assertion is
    what stops a member being added without a value exercising it, which is how
    `금월`, `내월` and `내달` were missing from the first draft.
    """
    from verinote.pipeline.query_measure_unit import (
        _MONTH_WORD_MEMBERS,
        korean_measure_unit_mismatch,
    )

    assert set(_MONTH_WORD_FIXTURES) == set(_MONTH_WORD_MEMBERS)
    for word, value in _MONTH_WORD_FIXTURES.items():
        assert korean_measure_unit_mismatch(_MONTH, value) is None, (word, value)


def test_every_month_part_has_a_date_it_reads():
    """Each month part still leaves the day attached to its month.

    `3월의 15일` and `3월 중 15일` are the fifteenth of March however the two are
    joined. Dropping any one member makes its row fire `('개월', '일')`.
    """
    from verinote.pipeline.query_measure_unit import (
        _MONTH_PART_MEMBERS,
        korean_measure_unit_mismatch,
    )

    assert set(_MONTH_PART_FIXTURES) == set(_MONTH_PART_MEMBERS)
    for part, value in _MONTH_PART_FIXTURES.items():
        assert korean_measure_unit_mismatch(_MONTH, value) is None, (part, value)


@pytest.mark.parametrize(
    "value",
    [
        "3월 내 15일 소요",
        "3월 후 15일 소요",
        "매월 약 3일",
        "전월 대비 3일 단축",
        "3월 계약 15일 소요",
        "매월 정기 3일 소요",
    ],
)
def test_a_word_outside_the_month_part_set_leaves_the_duration_readable(value):
    """What closing `_MONTH_PART_MEMBERS` buys, which the per-member test cannot.

    Every value here states a real duration with a month term standing earlier
    in it. Widen the part group to any single Hangul syllable and
    `3월 내 15일 소요`, `3월 후 15일 소요` and `매월 약 3일` go silent; widen it
    to any one or two syllables and every value here does, `전월 대비 3일 단축`
    and the two-syllable pair with it. So the closure is the whole precision of
    the branch rather than a tidiness preference.

    The cost of closing it is named in `_TIME_POINT`: `3월 중 15일 소요` reads as
    the fifteenth, and that is honestly ambiguous.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch(_MONTH, value) == ("개월", "일")


def test_a_clock_hour_is_a_point_in_time_and_시간_is_a_duration():
    """Both halves, because the branch and its lookahead fail differently.

    `시` is in neither the spellings table nor the counter list while `시간` is
    in both, so digits run into `시` can only be a clock. Drop the branch and the
    clock values fire; drop its `(?![가-힣])` and the durations and the ordinary
    words go silent instead.

    `3시30분` is here so that tightening the lookahead to `(?![가-힣0-9])` fails:
    half past three is written without a space as often as with one. The clock
    values are asked in `몇 시간` because that is the on-point relation, and it
    is safe from the same-unit suppressor -- that scan looks for the spelling
    `시간`, which `3시 30분` does not contain.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    for value in ["3시 30분", "오후 2시 15분", "14시 30분", "3시30분", "3시 30분 20초"]:
        assert korean_measure_unit_mismatch(_HOURS, value) is None, value
    for value in ["3시간", "24시간", "3시간 30분", "8시간 근무"]:
        assert korean_measure_unit_mismatch(_MONTH, value) == ("개월", "시간"), value
    for value in ["3시그마 3주", "5시리즈 3주"]:
        assert korean_measure_unit_mismatch(_MONTH, value) == ("개월", "주"), value


@pytest.mark.parametrize("value", ["123월 15일", "100월 3일", "1,234월 5일"])
def test_the_day_of_month_branch_takes_no_left_bound(value):
    """An absence pinned rather than a bound, because adding one is free.

    The branch this replaced had no `(?<![0-9])`, so `123월 15일` matches on its
    inner `23월 15일` and says nothing. Adding the bound would make these values
    newly caveated -- a caveat *gained*, which this guard may not do quietly, and
    which no before/after sweep catches unless its corpus happens to contain a
    three-digit month. An earlier revision of this change added the bound, and
    its own sweep reported no gained caveats -- wrongly, because the corpus had
    no such value in it.

    So the test exists for the edit a later author makes for free, and it fails
    in the direction that matters.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch(_MONTH, value) is None


def test_an_apostrophe_year_is_a_year_and_a_bare_two_digit_year_is_not():
    """The apostrophe is the whole of the difference, and both forms are read.

    `'21년` stands in for an elided century and no duration is written with one,
    so it is a date; bare `21년` really can be twenty-one years and is left
    reading YEAR, which `korean_measure_unit_mismatch` discloses.

    Three characters are read -- the straight `'`, the curly `’`, and the
    opening `‘` a word processor autocorrects a leading straight quote into --
    and each is pinned on its own, since dropping any one of them leaves the
    other two passing. `‘` was in the class for a round with no fixture behind
    it, which is the shape this file keeps having to close: a documented
    behaviour with no killer.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    for value in ["'21년", "’21년", "‘21년"]:
        assert korean_measure_unit_mismatch(_MONTH, value) is None, value
    for value in ["21년", "21년 계약"]:
        assert korean_measure_unit_mismatch(_MONTH, value) == ("개월", "년"), value


def test_a_point_in_time_silences_only_its_own_span():
    """The consequence of the span-local guard, re-derived not quoted.

    `_value_measure_units` drops the quantities overlapping a `_TIME_POINT`
    match and reports the rest, so the day inside each date below is silenced
    and the `3주` standing beside it is not -- for every month word and every
    month part, not the ones someone thought to list. The cross-product is built
    from the live tuples so that totality is derived here;
    `test_every_month_word_...` and `test_every_month_part_...` are what make
    the tuples complete, which is why this test depends on them.

    Asserted on the unit list rather than on the caveat, so it is a statement
    about the mechanism rather than about one question, and totality survives
    the move to span-local: a tuple member the pattern mishandles leaves its own
    day outside the span and surfaces here as `(("DAY", "일"), ("WEEK", "주"))`
    rather than as the week alone.
    """
    from verinote.pipeline.query_measure_unit import (
        _MONTH_PART_MEMBERS,
        _MONTH_WORD_MEMBERS,
        _value_measure_units,
    )

    composites = [f"{word} 3일, 3주 소요" for word in _MONTH_WORD_MEMBERS]
    composites += [f"3월 {part} 15일, 3주 소요" for part in _MONTH_PART_MEMBERS]
    composites += ["3시 시작, 3주 소요", "2021년 착수, 총 3주"]
    for value in composites:
        assert _value_measure_units(value) == (("WEEK", "주"),), value


@pytest.mark.parametrize(
    "value",
    ["2021년 3월 15일", "21년 3월 15일", "2021년 3월 15일 마감", "2021년 3월 중 15일",
     "2021년3월15일", "25년 12월 31일", "2021년 3월 15일자 계약"],
)
def test_a_year_in_front_of_a_date_is_inside_the_same_span(value):
    """The day branch's optional year prefix, and why span-local needs one.

    Without it, `2021년 3월 15일` is matched by the year+month branch at position
    zero. That branch stops at `3월`, and the day branch cannot take the value
    instead, because it must begin at the month term and the earlier match has
    already consumed past it -- so the `15일` stands outside every span and is
    read as fifteen days. Under the whole-value guard that cost nothing, since
    any match silenced the value whole; under span-local it is a wrong sentence.

    Asked in a unit none of these values states, so `_value_states_asked_unit`
    cannot be what produces the silence: `몇 주인가?` looks for `주` and finds
    none, and every unit here is in the same family as weeks, so anything left
    outside a span would be reported.

    Killer: delete the prefix.

    The prefix takes no left bound, and the last two rows are what pin that.
    `10000년 3월 15일` and `12021년 3월 15일` have the span open at their inner
    `0000년` and `2021년`, leaving a leading digit outside it that states
    nothing on its own, so both are silent. Add a `(?<![0-9])` inside the
    optional prefix group and the inner match is refused, the whole leading year
    falls outside every span, and each is told it states `년`.

    Two tests hold that property, not one, and it is worth knowing which because
    the answer changed once without anyone noticing. Under that bound these rows
    fail, and so does
    `test_a_quantity_is_dropped_by_overlap_and_not_by_masking_or_containment`,
    whose left-edge containment witness asserts
    `_value_measure_units("10000년 3월 15일") == ()` and reads `(("YEAR","년"),)`
    instead. That witness was added for the containment argument and holds this
    one as a side effect. Put the bound OUTSIDE the optional group rather than
    inside it and the day branch's own no-left-bound property goes with it,
    which is a different rule and has its own test -- so a mutation testing this
    sentence has to bound the prefix alone.

    These rows are asked in `몇 개월인가?` rather than `몇 주인가?` because what
    they would wrongly state is years, not days.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch("샘플계약의 기간은 몇 주인가?", value) is None


@pytest.mark.parametrize("value", ["10000년 3월 15일", "12021년 3월 15일"])
def test_the_year_prefix_takes_no_left_bound(value):
    """The other half of `test_a_year_in_front_of_a_date_is_inside_the_same_span`.

    Split out because the question differs: these state years rather than days,
    so they have to be asked in `몇 개월인가?` to be visible at all. The
    reasoning is in that test's docstring and in `_TIME_POINT`.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch(_MONTH, value) is None


def test_a_clock_time_carries_its_minute_and_second_inside_one_span():
    """The clock branch's optional minute and second, both sides of the rule.

    `3시 30분` is half past three. With the span ending at `3시` the `30분`
    outside it reads as thirty minutes, which asked in `몇 시간` is a wrong
    sentence, so the branch takes the minute and the second. The two tails are
    optional INDEPENDENTLY of each other, so `3시`, `3시 30분`, `3시 20초` and
    `3시 30분 20초` all match. Nesting the second inside the minute -- which is
    how this shipped first -- means a second can only follow a minute, and
    `3시 20초` then leaks its `초` and is read as twenty seconds. `-second` kills
    the `3시 30분 20초` and `3시 20초` rows and no others, and `-minute` kills the
    `30분` rows, so the two tails stay independently falsifiable.

    Only whitespace joins the components, and that is what keeps the branch
    honest rather than greedy: `회의 3시, 30분 소요` ends the clock at the comma,
    and the thirty minutes beside it are the duration they really are.
    `3시간 30분` is not a clock time at all -- `시간` is a unit and `시` is not --
    and stays a duration stating hours.

    The last row re-derives a claim `_TIME_POINT` makes rather than leaving it
    asserted. Neither tail carries a lookahead, and the day's
    `_DAY_DURATION_SUFFIXES` was the candidate weighed and declined: put it on
    the minute and `3시 30분간` is caveated `(시간, 분)` instead of silent.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    for value in ["3시 30분", "3시30분", "오후 2시 15분", "14시 30분", "3시 30분 20초",
                  "3시 5분 30초", "3시 30분쯤", "3시 30분 ~ 5시 45분",
                  "3시 20초", "3시20초"]:
        assert korean_measure_unit_mismatch(_HOURS, value) is None, value
    # Whitespace only, so a comma ends the clock and the duration beside it is read.
    assert korean_measure_unit_mismatch(_HOURS, "회의 3시, 30분 소요") == ("시간", "분")
    assert korean_measure_unit_mismatch(_MONTH, "3시간 30분") == ("개월", "시간")
    # The declined tail lookahead: with it, this row reads ("시간", "분").
    assert korean_measure_unit_mismatch(_HOURS, "3시 30분간") is None
    # And the direction that matters more -- borrowing it would not add a caveat
    # here, it would MOVE the existing correct one onto the clock's tail. This
    # row reads ("개월", "분") under the borrowed lookahead.
    assert korean_measure_unit_mismatch(_MONTH, "3시 30분간 3주") == ("개월", "주")
    # Both directions, because one alone reads as the whole rule. Order decides,
    # not presence: this row holds the SAME answer under the borrowed lookahead,
    # since the first same-family unit found is the `3주` standing in front.
    assert korean_measure_unit_mismatch(_MONTH, "3주 후 3시 30분간") == ("개월", "주")


def test_a_quantity_is_dropped_by_overlap_and_not_by_masking_or_containment():
    """Overlap-drop has two near neighbours, and each is a different rule.

    MASKING -- blank the span out of the string and re-scan -- would let
    `_VALUE_MEASUREMENT`'s trailing lookahead see the fill character instead of
    the real neighbour. In `3주2021-03-15` the lookahead refuses `3주` precisely
    because a digit follows it, and that refusal is the whole precision the fill
    would spend. Overlap-drop leaves every character where it was. The fill here
    is a space; NUL and any other character outside `[가-힣0-9A-Za-z]` read the
    same way, because it is the lookahead's class that decides.

    CONTAINMENT -- drop only the quantities a span fully covers, `ps <= start
    and end <= pe` -- is the nearer neighbour and the one a later tidying pass
    actually reaches, because it looks like a simpler way to write the same
    thing. It is not the same thing. A quantity can cross a span's boundary in
    either direction, and containment keeps every quantity that does.

    Both directions are asserted below, because one of them alone reads as a
    quirk of one branch. `2021-03-15일` crosses the RIGHT edge: the span is
    (0, 10) and `_VALUE_MEASUREMENT` reads `15일` at (8, 11), so containment
    reports the value as stating days -- precisely what the ISO branch exists to
    prevent, leaving the branch with no justification and
    `test_each_time_point_branch_is_needed_by_one_of_these_values` pinning a
    branch that no longer pays for itself. `10000년 3월 15일` crosses the LEFT
    edge: the year prefix takes no left bound, so the span opens at the inner
    `0000년` at (1, 13) while the real `10000년` runs (0, 6), and containment
    reports ten thousand years as a duration.

    The second is deliberately a widened-only witness. At the parent the day
    branch took no year, the span was `3월 15일` alone, and overlap and
    containment agreed on it -- so a probe run against the parent pattern
    under-reports the class. WHICH arguments it under-reports is worth naming
    rather than counting. The ISO branch, the flush-run-on residue
    (`12.5.33주`, `2021-03-153일`) and the day approximators (`3월 15일쯤`,
    `매월 15일정도`) already differ at the parent, so a parent-only probe sees
    them. The swallowed clock tail (`3시 30분간` and its siblings) and the year
    prefix's missing left bound differ only once the pattern is widened, so a
    parent-only probe sees neither. Those are the ones found so far rather than
    a closed set: any passage in `_TIME_POINT` whose value is silent because a
    quantity STRADDLES a span rests on this difference, since containment would
    report that quantity. A quantity wholly inside a span is dropped either way
    -- `매월 15일`, `3시 30분`, `2021년` -- so those passages rest on nothing here.

    Both alternative readings are recomputed inline and asserted to DIFFER from
    the shipped one. That is the point of the test: without the differ-assertion
    each half would restate the implementation instead of discriminating it.
    """
    from verinote.pipeline.query_measure_unit import (
        _MEASUREMENT_UNIT_SPELLINGS,
        _TIME_POINT,
        _VALUE_MEASUREMENT,
        _value_measure_units,
    )

    def read(value, keep):
        return tuple(
            (_MEASUREMENT_UNIT_SPELLINGS[m.group("unit")], m.group("unit"))
            for m in _VALUE_MEASUREMENT.finditer(value)
            if keep(m.start(), m.end(), [p.span() for p in _TIME_POINT.finditer(value)])
        )

    def masked_reading(value):
        chars = list(value)
        for point in _TIME_POINT.finditer(value):
            chars[point.start() : point.end()] = " " * (point.end() - point.start())
        return tuple(
            (_MEASUREMENT_UNIT_SPELLINGS[m.group("unit")], m.group("unit"))
            for m in _VALUE_MEASUREMENT.finditer("".join(chars))
        )

    def contained_reading(value):
        return read(value, lambda s, e, pts: not any(ps <= s and e <= pe for ps, pe in pts))

    for value in ["3주2021-03-15", "2021-03-153주"]:
        assert _value_measure_units(value) == (), value
        assert masked_reading(value) == (("WEEK", "주"),), value

    # Straddling the span's RIGHT edge: the quantity begins inside and ends
    # outside, so containment keeps it and the ISO branch loses its purpose.
    assert _value_measure_units("2021-03-15일") == ()
    assert contained_reading("2021-03-15일") == (("DAY", "일"),)
    assert [p.span() for p in _TIME_POINT.finditer("2021-03-15일")] == [(0, 10)]
    assert [m.span() for m in _VALUE_MEASUREMENT.finditer("2021-03-15일")] == [(8, 11)]

    # And the LEFT edge, which is the mirror case and not an ISO curiosity: the
    # year prefix carries no left bound, so the span starts at the date's inner
    # `0000년` and the real `10000년` begins before it and ends inside it. This
    # witness exists only under the widened pattern -- at the parent the day
    # branch took no year, the span was `3월 15일` alone, and the two rules
    # agreed -- so probing the parent alone would report no difference here.
    assert _value_measure_units("10000년 3월 15일") == ()
    assert contained_reading("10000년 3월 15일") == (("YEAR", "년"),)
    assert [p.span() for p in _TIME_POINT.finditer("10000년 3월 15일")] == [(1, 13)]
    assert [m.span() for m in _VALUE_MEASUREMENT.finditer("10000년 3월 15일")] == [
        (0, 6),
        (10, 13),
    ]


def test_a_span_covers_every_component_a_value_could_be_said_to_state():
    """The closure the span-local guard rests on, checked from both sides.

    A component of a point-in-time expression can only cost a caveat if it can
    be read as a unit, and `_MEASUREMENT_UNIT_SPELLINGS` is what decides that.
    The calendar's and the clock's components are year, month, day, hour, minute
    and second -- not a lexical class the next input extends -- so intersecting
    those six with the live table gives the set every span has to reach.

    The two sides are checked separately because only one of them is live in the
    same sense. Adding `월` or `시` to the spellings table changes the
    intersection and fails the first half for want of a fixture. Adding a
    `_TIME_POINT` branch does not touch the component list at all, so that side
    is pinned from the pattern instead: the unit spellings occurring literally
    in `_TIME_POINT.pattern` are a computable set, and a branch naming a sixth
    -- `주`, say, for a week of the month -- fails the second half.

    `달` is in that pattern set only through the members of
    `_MONTH_WORD_MEMBERS` that spell it, and it needs its own fixture for a
    different reason from the four: it is not a component of the expression at
    all, it is part of the word naming the month. Every such member writes a
    Hangul syllable in front of `달` with at most a space between -- `매달` and
    `내달` flush, `다음 달`, `이번 달` and `지난 달` spaced -- and
    `_VALUE_MEASUREMENT` needs a digit run there, so no `달` inside a span can be
    read as a unit. That is derived below over the live tuple rather than
    asserted, because a member spelling it otherwise would make the sentence
    false and nothing would notice.

    Both halves of the first check, and the `달` check, carry a non-vacuity
    control: each probe is shown to state its spelling OUTSIDE the guard before
    it is shown to be silent inside it, so a probe the pattern happens to mangle
    cannot pass by stating nothing.
    """
    from verinote.pipeline.query_measure_unit import (
        _MEASUREMENT_UNIT_SPELLINGS,
        _MONTH_WORD_MEMBERS,
        _TIME_POINT,
        _VALUE_MEASUREMENT,
        _value_measure_units,
    )
    from verinote.text import nfc

    # The probes are the CROSS-PRODUCT of the optional components, not one
    # maximal expression per family. A leak lives in the sparse subsets: while
    # the clock's second was nested inside its minute, `3시 30분 20초` was silent
    # and `3시 20초` -- the same components with the minute absent -- leaked its
    # `초`. Probing only the maximal form could not see that.
    clock = [f"3시{m}{s}" for m in ["", " 30분"] for s in ["", " 20초"]]
    date = [f"{y}3월{p} 15일" for y in ["", "2021년 "] for p in ["", " 중"]]
    assert len(clock) == len(date) == 4, (clock, date)

    components = {"년": date, "월": date, "일": date,
                  "시": clock, "분": clock, "초": clock}
    readable = {c for c in components if c in _MEASUREMENT_UNIT_SPELLINGS}
    assert readable == {"년", "일", "분", "초"}
    for component in readable:
        # Non-vacuity per subset: the component must be genuinely readable
        # outside the guard in at least one probe, or "silent inside it" says
        # nothing. `3시` states no 분, so the check is existential over the
        # subsets and the silence check is universal over them.
        stated = [
            probe for probe in components[component]
            if component in {m.group("unit")
                             for m in _VALUE_MEASUREMENT.finditer(nfc(probe).casefold())}
        ]
        assert stated, (component, components[component])
        for probe in components[component]:
            assert _value_measure_units(probe) == (), (component, probe)

    pattern_side = {s for s in _MEASUREMENT_UNIT_SPELLINGS if s in _TIME_POINT.pattern}
    assert pattern_side == {"년", "달", "분", "일", "초"}

    # `달`'s fixture: every member spelling it puts a Hangul syllable in front,
    # so no span can state months. The control is what stops this being vacuous
    # -- `달` really is a unit spelling this file reads.
    spelling_달 = [w for w in _MONTH_WORD_MEMBERS if "달" in w]
    assert spelling_달, "no member spells 달, so the pattern-side set is stale"
    # The word members are the ONLY route `달` takes into the pattern: strike
    # them out and none is left. A branch spelling `달` after a digit would
    # survive this and is what it is here to catch, since the pattern-side set
    # above cannot see a second route to a spelling already in it.
    without_words = _TIME_POINT.pattern
    for member in _MONTH_WORD_MEMBERS:
        without_words = without_words.replace(member.replace(" ", r"\s*"), "")
    assert "달" not in without_words
    for member in spelling_달:
        before = member[: member.index("달")].rstrip()
        assert before and "가" <= before[-1] <= "힣", member
        assert _value_measure_units(f"{member} 15일") == (), member
    assert _value_measure_units("3달") == (("MONTH", "달"),)


def test_the_gained_caveats_are_the_ones_standing_outside_a_span():
    """What #452 recovers, and the two silences it deliberately keeps.

    Each value below states a duration that stands clear of every point-in-time
    span in it, and every one of them was silent under the whole-value guard.
    The first four are #452's own witnesses; the rest are the shapes the other
    tests used to carry as costs.

    The two silences are decisions rather than residue. `매월 3일 소요` is the
    third of each month -- the day branch reads a bound duration suffix and
    nothing else, and `소요` is a free word after a space -- so the whole of
    `매월 3일` sits inside the span and there is nothing outside it to report.
    Reading it as three days a month means consulting free words, the open class
    #450 closed against. `매월 15일 동안` is #458's shape and is unchanged: the
    `15일` is read and then dropped for overlapping the day's span, and reaching
    it means moving the lookahead's position rather than making the guard
    span-local.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    for value in ["2021년 착수, 총 3주", "2024/01/02 3주", "12.5.3 버전, 3주 소요",
                  "3시 시작, 3주 소요", "'21년 시작, 3주", "다음 달 3일 회의, 3주 소요",
                  "3월 15일자 계약, 3주 소요", "매월 15일부터 3주간 진행",
                  "매월 15일 동안 3주", "매월 15일동안 3주"]:
        assert korean_measure_unit_mismatch(_MONTH, value) == ("개월", "주"), value
    assert korean_measure_unit_mismatch(_MONTH, "3월 15일까지 2주 연장") == ("개월", "주")
    assert korean_measure_unit_mismatch(_MONTH, "2021-03-15 (3일)") == ("개월", "일")
    assert korean_measure_unit_mismatch(_HOURS, "2021년 기준 30분") == ("시간", "분")
    # Kept silent on purpose, and each for its own reason.
    assert korean_measure_unit_mismatch(_MONTH, "매월 3일 소요") is None
    assert korean_measure_unit_mismatch(_MONTH, "매월 15일 동안") is None


def test_whether_a_withdrawn_cover_becomes_a_caveat_depends_on_the_question():
    """The withdrawal is total; the outcome is not. Same value, two questions.

    `_TIME_POINT`'s cost paragraph says span-local removes the accidental cover
    from every recorded wrong sentence at once, and that a caveat then appears
    is a separate matter decided by `korean_measure_unit_mismatch`'s own tests.
    This is that claim's fixture, and it uses ONE value under two questions so
    the asymmetry cannot be read as a property of the value.

    Asked in `몇 년인가?` the value is silent, and not because of this guard:
    `_value_states_asked_unit` reads the head's own `2021년` as years, which is
    the answer it gave before #452 as well. Asked in `몇 개월인가?` nothing
    suppresses, the `100주` stands outside the span, and the caveat fires --
    wrongly, since `100주` is one hundred shares, which is why the value is a
    row in `test_known_false_unit_statements_are_recorded_not_fixed`.

    `3시 5분` is the control and the opposite shape: silent under every question
    the rule can put, because the `5분` is the clock's own minute and lies
    inside the span. A reader who saw only the first witness would take the
    head as what decides; a reader who saw only the second would take the
    outcome for uniform.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch("샘플계약의 기간은 몇 년인가?", "2021년, 100주") is None
    assert korean_measure_unit_mismatch(_MONTH, "2021년, 100주") == ("개월", "주")
    for question in [_MONTH, _HOURS, "샘플계약의 기간은 몇 년인가?", "샘플작업의 시간은 몇 초인가?"]:
        assert korean_measure_unit_mismatch(question, "3시 5분") is None, question
    # The third mechanism, which produces a wrong NAME rather than a silence:
    # the `15일` is reported and never reached, because `3주` precedes it.
    assert korean_measure_unit_mismatch(_MONTH, "2021년 계약, 3주 소요, 15일 마감") == (
        "개월",
        "주",
    )
    assert korean_measure_unit_mismatch(_MONTH, "2021년 계약, 15일 마감") == ("개월", "일")
    # The family filter, which `_TIME_POINT` derives from the loop's exit rather
    # than listing as a mechanism: a quantity can be reported, unsuppressed and
    # first and still name nothing, because it measures something the question
    # did not ask about. One value under two families, twice over. Without these
    # rows every question in this test is a time question and the family filter
    # has no negative candidate that could fire -- which is exactly how it went
    # unnoticed while the list was being called complete.
    _MONEY = "샘플사업의 가격은 몇 원인가?"
    assert korean_measure_unit_mismatch(_MONEY, "2021년, 15,000달러 계약") == (
        "원",
        "달러",
    )
    assert korean_measure_unit_mismatch(_MONTH, "2021년, 15,000달러 계약") is None
    # The reverse pairing, and the witness `_TIME_POINT` cites: a DAY reported
    # and outside every span, passed over because the question asks in won.
    assert korean_measure_unit_mismatch(_MONEY, "2021년, 15일 마감") is None
    assert korean_measure_unit_mismatch(_MONTH, "2021년, 15일 마감") == ("개월", "일")


def test_every_point_in_time_in_a_value_gets_its_own_span():
    """`finditer`, not `search`: each quantity is tested against all the spans.

    A value can hold more than one point in time, and the spans are
    non-overlapping and left to right. `2021년 3월 15일 10시 30분 회의, 3주 소요`
    holds two -- the date and the clock time -- and the `3주` is outside both.

    Falsifiable in two directions at once, which is why this row is worth having
    beside the single-span ones. Without the day branch's year prefix the date's
    own `15일` escapes and the caveat reads `(개월, 일)`; without the clock's
    minute tail the `30분` escapes and it reads `(개월, 분)`. Those are the two it
    catches, and not more: dropping the second tail, re-nesting the tails, or
    widening the day's lookahead all still name the weeks here, because this
    value has no bare second and no Hangul-tailed day. Two directions, named,
    rather than a claim about every mutation.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch(_MONTH, "2021년 3월 15일 10시 30분 회의, 3주 소요") == (
        "개월",
        "주",
    )


@pytest.mark.parametrize(
    "value",
    [
        "2년", "6개월", "3주", "3일", "30분", "45초", "3시간", "2년 6개월",
        "3개월 15일", "12년 6개월", "90일", "24개월", "1년 6개월", "3일간", "2주일",
        "1.5일", "50/15일", "10/30일",
    ],
)
def test_a_duration_is_still_read_as_a_quantity(value):
    """The other side of the guard: widening it must not eat ordinary durations.

    Asserted on the reading rather than on the caveat, and that is the point of
    the test. For `3개월 15일`, `2년 6개월`, `24개월` and `1년 6개월` the answer to
    `몇 개월인가?` is None either way, so only the unit list distinguishes "the
    guard ate it" from "the same-unit suppressor silenced it".

    `1.5일`, `50/15일` and `10/30일` are here because a dotted or slashed month
    term was considered for #450 and withdrawn -- each of these would have
    stopped being read.
    """
    from verinote.pipeline.query_measure_unit import _value_measure_units

    assert _value_measure_units(value) != ()


def test_a_month_word_written_without_its_space_is_still_a_month_term():
    """`다음 달` is joined with `\\s*`, not literally, and that is load-bearing.

    `_MONTH_OF_YEAR`'s comment says the space is relaxed so `다음달 1일` reads
    like `다음 달 1일`. Join the members literally instead and the unspaced form
    stops being a date, which nothing else in the suite notices.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    for value in ["다음달 1일", "다음달 12일", "이번달 1일", "지난달 15일"]:
        assert korean_measure_unit_mismatch(_MONTH, value) is None, value


def test_the_day_number_is_one_or_two_digits_and_that_width_is_load_bearing():
    """Unlike the month term's width, the day's is not decoration.

    A month term and a clock hour sit at the start of their branch with no left
    bound, so a longer digit run just matches further in and the quantifier
    beside them reads the same values whatever it admits. The day number cannot
    do that -- it has to begin where the month term ended -- so widening it to
    three digits makes `다음달 123일` a date, and narrowing it to one makes
    `매월 15일` stop being one.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch(_MONTH, "다음달 123일") == ("개월", "일")
    assert korean_measure_unit_mismatch(_MONTH, "매월 15일") is None


def test_whitespace_may_stand_between_the_day_number_and_its_일():
    """The `\\s*` before `일`, which no other fixture exercises.

    `매월 2 일` is spaced the way a value typed with a stray space is, and drops
    out of the day branch entirely without it.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    for value in ["매월 2 일", "3월 15 일", "다음 달 1 일"]:
        assert korean_measure_unit_mismatch(_MONTH, value) is None, value


@pytest.mark.parametrize("value", ["1차월 3일 소요", "2차월 15일 소요", "3차월 5일"])
def test_a_month_word_preceded_by_a_digit_is_not_a_month_term(value):
    """`1차월` is month one of a programme, not next month.

    The word alternatives carry a `(?<![0-9])` that the digit month must not
    have. Without it `차월` matches inside `1차월` and the value is read as a
    point in time, losing a caveat it earned -- and unlike the disclosed losses,
    there is no point in time anywhere in the value, so the standing
    "a value that also carries a point in time" rule does not cover it.

    Digits are the only thing the bound excludes. `해당월 15일` and `익익월 15일`
    keep matching on their tails, which is an accident that happens to land on
    the right reading; a Hangul bound would give that up, so it is not taken.

    The same narrowness leaves the Sino-Korean numeral spelling out: `일차월`
    and `이차월` are silenced exactly as `1차월` was, and this test does not
    reach them. So the class is not closed -- the digit spelling is handled and
    the numeral one is not, and no bound of this shape can take both without
    losing `해당월`.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch(_MONTH, value) == ("개월", "일")
    assert korean_measure_unit_mismatch(_MONTH, "해당월 15일") is None


@pytest.mark.parametrize(
    "value",
    ["매월 15일간", "전월 10일간", "다음 달 10일간 휴무", "3월 초 15일간",
     "이번 달 3일간 점검", "금월 5일간 휴무", "3월 15일간",
     "매월 15일가량", "3월 15일짜리", "다음 달 3일짜리 점검"],
)
def test_a_day_wearing_a_duration_suffix_is_a_duration_not_a_day_of_the_month(value):
    """A `_DAY_DURATION_SUFFIXES` tail makes `N일...` a duration, not a date.

    `_UNIT_SUFFIX` reads `15일간` as fifteen days -- that is what the suffix is
    for -- and `15일가량` and `15일짜리` the same. So a value saying `매월 15일간`
    states a duration and nothing in it is a point in time. Without the day
    branch's lookahead the branch matches the `15일` inside, and the quantity
    `_VALUE_MEASUREMENT` reads there overlaps that span and is dropped -- a
    caveat lost with no point in time to justify it. Under the whole-value
    guard the loss reached the rest of the value too; span-local confines it to
    the day, and the lookahead is still what keeps even that.

    Excluding only `간` would fix a third of the class and leave its siblings
    failing the same way -- the "narrows one shape and leaves its neighbours"
    defect #450 exists to stop. But the set is not all of `_UNIT_SUFFIX_MEMBERS`
    either: `쯤` and `정도` approximate rather than quantify, and the test below
    is the other half of that partition.

    The rows with a digit month also move relative to #445, not only to #450:
    the older guard silenced `3월 15일간` too, so this lookahead reaches back
    past #450's own additions and makes them newly caveated. That is deliberate,
    and within #450 it was the only place a value gained a caveat rather than
    losing one -- #452 is not so confined, since making the guard span-local
    gains caveats wherever a duration stands outside a span. Which rows those
    are is derived in
    `test_the_only_caveats_gained_are_a_digit_month_wearing_a_unit_suffix`
    rather than counted here, because the count depends on how many digit
    months one chooses to write down and the class does not.
    """
    from verinote.pipeline.query_measure_unit import (
        _DATE_APPROXIMATOR_SUFFIXES,
        _DAY_DURATION_SUFFIXES,
        _UNIT_SUFFIX_MEMBERS,
        _value_measure_units,
        korean_measure_unit_mismatch,
    )

    # The tripwire, and it only works because BOTH halves are literal. Derive
    # either one and this equality holds by construction: with `께` added to
    # `_UNIT_SUFFIX_MEMBERS` and the approximators computed as the complement,
    # this assertion and the disjointness below both pass and nothing notices.
    # A hardcoded copy of `_UNIT_SUFFIX_MEMBERS` would also catch that, but it
    # duplicates the tuple without saying what the duplicate is for; this says
    # the two halves must together account for the whole, which is the decision
    # a new member forces someone to make.
    assert set(_DAY_DURATION_SUFFIXES) | set(_DATE_APPROXIMATOR_SUFFIXES) == set(
        _UNIT_SUFFIX_MEMBERS
    )
    assert not set(_DAY_DURATION_SUFFIXES) & set(_DATE_APPROXIMATOR_SUFFIXES)
    assert korean_measure_unit_mismatch(_MONTH, value) == ("개월", "일")
    assert ("DAY", "일") in _value_measure_units(value)


@pytest.mark.parametrize(
    "value",
    ["매월 15일", "3월 중 15일", "3월 말 15일", "다음 달 1일", "3월 초 5일",
     "3월 15일자", "매월 15일부터", "매월 25일까지 납부"],
)
def test_a_day_of_the_month_without_a_unit_suffix_is_still_a_date(value):
    """The other side of the lookahead: it must not cost the dates the branch is for.

    Paired with the test above so that a regression in either direction fails,
    rather than one that silently trades the classes against each other.

    Every value here states nothing but its date, and that is the whole of what
    this test can show since #452. Three values carrying a duration as well
    (`3월 15일자 계약, 3주 소요` and two like it) used to stand here as the reason
    the lookahead may not be widened to any Hangul tail: under the whole-value
    guard, reading `3월 15일자` as not-a-date unblocked the `3주` beside it. Under
    span-local the `3주` is reported either way, so those three no longer
    discriminate anything and have moved to
    `test_the_gained_caveats_are_the_ones_standing_outside_a_span` as recovery
    rows. What still refuses the widened lookahead is
    `test_a_day_wearing_an_approximator_is_still_a_date`, whose `3월 15일쯤` and
    `매월 15일정도` are caveated `(개월, 일)` under it and silent as written.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch(_MONTH, value) is None


def test_the_only_caveats_gained_are_a_digit_month_wearing_a_unit_suffix():
    """The gained class, derived from the live tuples rather than counted.

    #450 is otherwise a change that only removes caveats. The day branch's
    suffix lookahead is the exception: it reaches back past #450's own
    additions and makes `3월 15일간` newly caveated, because the guard shipped in
    #445 silenced that too. This asserts what the exception is, in both
    directions -- everything gained has a digit month, nothing with a word month
    or a month part gains, and every gained value is duration-tailed rather than
    approximator-tailed, which is what makes the added caveat correct.

    What the baseline here IS, stated precisely, because it is not a commit.
    Only the PATTERN is substituted -- `e7ac2a7`'s, written out below -- while
    consumption stays span-local, the way `_value_measure_units` reads today.
    That combination never shipped: at `e7ac2a7` the guard was whole-value. It
    is the right comparison anyway, because it isolates the one variable this
    test is about, which is which shapes the pattern calls a date. Read the
    result as "the pattern's additions gain these caveats", not as "the release
    gained these", and do not take the substituted build as a description of
    any past behaviour.

    That baseline is why only a digit month can gain: `e7ac2a7`'s pattern had no
    word months, so it never made one a date and has none to stop making one.
    Measured instead against today's pattern with the lookahead removed, any
    month term gains. `_TIME_POINT` states both and names which is which;
    neither reading is the other's contradiction.

    Derived and not counted, deliberately. A count here measures how many digit
    months the cross-product happens to enumerate, not anything about the code:
    two digit months and five suffixes give ten, twelve digit months would give
    sixty, and neither number says more than the shape does. Two earlier
    attempts at this claim were corpus counts, and the first was wrong because
    its corpus carried the `간` shape and not the other four.

    The #445 pattern is written out because it is history and cannot drift; the
    tuples it is compared against are live.
    """
    import itertools
    import re

    from verinote.pipeline.query_measure_unit import (
        _DATE_APPROXIMATOR_SUFFIXES,
        _DAY_DURATION_SUFFIXES,
        _MONTH_PART_MEMBERS,
        _MONTH_WORD_MEMBERS,
        _TIME_POINT,
        _UNIT_SUFFIX_MEMBERS,
        _value_measure_units,
    )
    import verinote.pipeline.query_measure_unit as qmu

    shipped = re.compile(
        r"[0-9]{1,2}\s*월\s*[0-9]{1,2}\s*일"
        r"|(?<![0-9])[0-9]{2,4}\s*년\s*[0-9]{1,2}\s*월"
        r"|(?<![0-9])[0-9]{4}\s*년(?![0-9])"
        r"|(?<![0-9])[0-9]{2,4}\s*[-./]\s*[0-9]{1,2}\s*[-./]\s*[0-9]{1,2}"
    )
    digit_months = ("3월", "12월")
    terms = digit_months + tuple(_MONTH_WORD_MEMBERS)
    values = [
        " ".join(piece for piece in (term, part, f"15일{suffix}") if piece)
        for term, part, suffix in itertools.product(
            terms, ("",) + tuple(_MONTH_PART_MEMBERS), _UNIT_SUFFIX_MEMBERS
        )
    ]

    def caveat(pattern, value):
        original = qmu._TIME_POINT
        qmu._TIME_POINT = pattern
        try:
            return qmu.korean_measure_unit_mismatch(_MONTH, value)
        finally:
            qmu._TIME_POINT = original

    gained = [
        value for value in values
        if caveat(shipped, value) is None and caveat(_TIME_POINT, value) is not None
    ]
    assert gained, "no gained values means the cross-product missed the shape"
    # Nothing with a word month or a month part gains. Checked before the
    # unpack below, because both of those shapes split into three and would
    # raise ValueError there instead of failing this assertion -- the worse
    # diagnostic for exactly the regression this guards.
    assert not [
        value for value in gained
        if any(word in value for word in _MONTH_WORD_MEMBERS)
        or any(part in value.split()[1:-1] for part in _MONTH_PART_MEMBERS)
    ]
    for value in gained:
        term, day = value.split()
        assert term in digit_months, value
        # The correctness half, and it has to be falsifiable. Asserting that the
        # value states DAY cannot fail here: `일` is the only unit anything in
        # this cross-product contains, so it passed for `3월 15일쯤` too, where
        # the caveat was wrong. What makes a gained caveat right is the tail
        # being a duration suffix rather than an approximator, so that is what
        # is asserted -- and widening the lookahead back over
        # `_DATE_APPROXIMATOR_SUFFIXES` fails here.
        assert day.endswith(_DAY_DURATION_SUFFIXES), value
        assert not day.endswith(_DATE_APPROXIMATOR_SUFFIXES), value
        assert ("DAY", "일") in _value_measure_units(value), value


@pytest.mark.parametrize(
    "value",
    ["3월 15일쯤", "매월 15일정도", "매월 15일쯤", "3월 중 15일정도", "다음 달 1일쯤",
     "3월 15일경", "3월 15일 쯤", "매월 15일 정도"],
)
def test_a_day_wearing_an_approximator_is_still_a_date(value):
    """`_DATE_APPROXIMATOR_SUFFIXES`: the other half of the partition.

    `쯤` and `정도` sit in `_UNIT_SUFFIX_MEMBERS` because they leave a quantity
    readable -- `15일쯤` reads as days -- but that is the only property that
    tuple claims. The day branch needs the converse, and an approximated date is
    still a date, so reading `매월 15일쯤` as fifteen days would un-fix this
    issue's own headline one particle away from `매월 15일`.

    The file already agrees, and `3월 15일경` is the proof: `경` is the standard
    date approximator, sits in no tuple, and is read as a date. Two spellings of
    one meaning must not get two answers.

    `3시경` is deliberately NOT cited here. It is silent, but by stating no unit
    rather than by being read as a point in time -- `_TIME_POINT` does not match
    it at all, as the clock paragraph says. Citing it would repeat the exact
    confusion this module keeps warning against.

    The last two are the same values spaced. `_VALUE_MEASUREMENT` reads `15일쯤`
    and `15일 쯤` alike, so a lookahead that split them would make a space decide
    the verdict -- the defect `_VALUE_MEASUREMENT_RELAXED` exists to fix, one
    branch over.
    """
    from verinote.pipeline.query_measure_unit import (
        _DATE_APPROXIMATOR_SUFFIXES,
        _UNIT_SUFFIX_MEMBERS,
        korean_measure_unit_mismatch,
    )

    assert set(_DATE_APPROXIMATOR_SUFFIXES) < set(_UNIT_SUFFIX_MEMBERS)
    assert korean_measure_unit_mismatch(_MONTH, value) is None


@pytest.mark.parametrize("marker", ["동안", "남짓", "내내", "이상"])
@pytest.mark.parametrize("month_word", ["매월", "전월", "다음 달"])
def test_a_free_word_duration_marker_is_not_consulted(month_word, marker):
    """The boundary, asserted rather than assumed: only bound suffixes count.

    `매월 15일 동안` is fifteen days, and this rule silences it. The day branch
    reads what is written flush against `일`, and a free word after a space is
    not consulted, so the value is a day of the month with a word after it.

    Recorded as a cost, not fixed. "Means a duration" is an open lexical class --
    these, and any other free word implying a span -- so enumerating it would
    narrow one shape and leave its neighbours, the failure #450 exists to stop.
    "Is bound to the number" is closed because every candidate is a member of
    `_UNIT_SUFFIX_MEMBERS`, which is enumerated; `_DAY_DURATION_SUFFIXES` is the
    part of it that survives both filters, not the set of bound suffixes -- `쯤`
    is bound and is not in it. That is the trade, and this test is where it is
    visible instead of implied.

    Every marker here is a free word whose standard spelling is spaced, and each
    was checked against both dimensions the caveat level cannot see: whether the
    spaced form is the standard one, and whether it attaches to a point in time
    (`3시X`). `여` sat here for a round and satisfies neither -- it is a 접미사
    on the NUMBER, as in `15여 일`, so `매월 15일 여` is not Korean and its
    silence says nothing about this boundary. `째` fails the same way flush
    against the counter (`15일째`), and `내외` and `가까이` are spaced but read a
    clock as readily as a span, which is the `이내` defect. None of them was
    taken as a replacement; the list is explicitly not exhaustive, so a fifth
    member has to earn its place rather than fill a slot.

    The values really do state days; the guard is what hides them, which the
    second assertion shows by asking the same question of the bound spelling.

    What #452 changed is the reach of the loss, not the loss. The guard used to
    be whole-value, so a day read as a date silenced every other quantity in the
    value and `매월 15일동안 3주` said nothing about its three weeks either; the
    span-local guard drops only what overlaps the date, so the three weeks are
    reported and the third assertion records that. The day itself is still lost,
    which is why #458 is narrowed rather than closed.

    Two mechanisms produce that one silence and they are worth telling apart,
    since conflating them is the mistake this file keeps having to correct. In
    the spaced `매월 15일 동안` the `15일` IS matched by `_VALUE_MEASUREMENT` and
    is then dropped for overlapping the day's span. In the flush
    `매월 15일동안` no quantity is read at all, because the trailing lookahead
    refuses a unit run into Hangul. Same outcome, different cause, and only the
    first would move if the span moved -- which is what #458 asks for, and what
    listing the free words would not buy, since standard orthography spaces
    them and a lookahead flush against `일` cannot reach past a space.
    """
    from verinote.pipeline.query_measure_unit import korean_measure_unit_mismatch

    assert korean_measure_unit_mismatch(_MONTH, f"{month_word} 15일 {marker}") is None
    assert korean_measure_unit_mismatch(_MONTH, f"{month_word} 15일간") == ("개월", "일")
    # The whole-value spillover, which #452 ended: the day is lost, the week is not.
    assert korean_measure_unit_mismatch(_MONTH, f"{month_word} 15일{marker} 3주") == (
        "개월",
        "주",
    )


def test_only_a_suffix_bound_to_the_day_changes_the_verdict():
    """The bound/free split itself, over the live tuple.

    Every `_DAY_DURATION_SUFFIXES` member flush against `일` makes the value a
    duration; the same member after a space does not. That is a documented
    boundary rather than an accident: `15일간` is standard orthography and
    `15일 간` is a misspelling, so the two spellings are not equally admissible
    and reading them differently is the rule working.

    It is NOT the `3시간30분` defect one branch over. There both spacings are
    standard, which is what made a space-dependent verdict wrong. The control is
    `정도`, a free noun whose spaced form is the standard one -- and it shows no
    flip, because the approximators are not consulted either way.
    """
    from verinote.pipeline.query_measure_unit import (
        _DATE_APPROXIMATOR_SUFFIXES,
        _DAY_DURATION_SUFFIXES,
        korean_measure_unit_mismatch,
    )

    assert _DAY_DURATION_SUFFIXES, "an empty set would make this vacuous"
    for suffix in _DAY_DURATION_SUFFIXES:
        assert korean_measure_unit_mismatch(_MONTH, f"매월 15일{suffix}") == ("개월", "일")
        assert korean_measure_unit_mismatch(_MONTH, f"매월 15일 {suffix}") is None
    for suffix in _DATE_APPROXIMATOR_SUFFIXES:
        assert korean_measure_unit_mismatch(_MONTH, f"매월 15일{suffix}") is None
        assert korean_measure_unit_mismatch(_MONTH, f"매월 15일 {suffix}") is None


# --- the boundary between the two modules (#459) ----------------------------


def test_query_intent_never_imports_the_measure_unit_module():
    """The dependency runs one way, and only a static check can hold it there.

    `query_measure_unit` imports three names from `query_intent`. A re-export
    added the other way -- `from verinote.pipeline.query_measure_unit import
    korean_measure_unit_mismatch` in `query_intent`, to spare a caller the new
    address -- closes the cycle. Measured, mutant applied in place:

    | placement in query_intent.py | entry point                  | observed   |
    | top, beside the other import | import verinote.pipeline.ask | ImportError|
    | top                          | import ...query_intent       | ImportError|
    | top                          | import ...query_measure_unit | ImportError|
    | bottom, after every def      | import verinote.pipeline.ask | passes     |
    | bottom                       | import ...query_intent       | passes     |
    | bottom                       | import ...query_measure_unit | passes     |

    The bottom row is why this test exists rather than a smoke import. Bottom
    placement passes at every entry point, including this file's -- with the
    re-export in place `pytest tests/test_query_measure_unit.py` alone was 227
    passed. It is green because `verinote/pipeline/__init__.py` imports
    `verinote.pipeline.query`, which imports `query_intent`, so the package
    initialiser finishes `query_intent` before any submodule body runs and
    nothing here ever reaches a partially initialised module. No entry point in
    this layout loads `query_measure_unit` first.

    So a re-export placed at the bottom produces no red anywhere, and a green
    suite is not evidence that the direction held. That is the whole cost of
    deleting this test: the cycle it forbids is one an author would only find
    after the import graph had already grown back the shape #459 cut.

    Parsed rather than imported, so a re-export inside `if TYPE_CHECKING` or
    behind a function is caught too -- those never execute and so could never
    fail an import probe.
    """
    import ast
    import pathlib

    from verinote.pipeline import query_intent

    source = pathlib.Path(query_intent.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").endswith("query_measure_unit"):
                offenders.append((node.lineno, node.module))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.endswith("query_measure_unit"):
                    offenders.append((node.lineno, alias.name))

    assert offenders == [], (
        "query_intent must not import query_measure_unit; the dependency runs "
        f"the other way. Found: {offenders}"
    )
