# SPDX-License-Identifier: MPL-2.0
"""The unit a measure question asks in, and whether the answer states another."""

from __future__ import annotations

import re

from verinote.pipeline.query_intent import (
    _KOREAN_ATTRIBUTE_LABEL_MEASURE_TAIL,
    _KOREAN_ATTRIBUTE_QUESTION,
    _label_readings_after_measure,
)
from verinote.text import nfc


_MEASUREMENT_FAMILY = {
    "YEAR": "time", "MONTH": "time", "WEEK": "time", "DAY": "time",
    "HOUR": "time", "MINUTE": "time", "SECOND": "time",
    "PERCENT": "ratio", "TIMES": "ratio",
    "KRW": "money", "USD": "money", "JPY": "money", "EUR": "money",
}
"""The quantity each canonical unit measures.

Two units in one family measure the same thing, so asking in one and being
answered in the other is a mismatch a reader can act on. Two units in different
families are not comparable at all: `몇 원인가?` answered `2년` is a money
question answered with a duration, which is a mis-read question or a mis-stored
fact rather than a unit difference, and saying "no unit conversion is applied"
would be the wrong thing to tell that reader. So the cross-family case is
silent.
"""

_MEASUREMENT_UNIT_SPELLINGS = {
    "년": "YEAR", "연": "YEAR", "살": "YEAR", "세": "YEAR", "year": "YEAR", "years": "YEAR",
    "개월": "MONTH", "달": "MONTH", "month": "MONTH", "months": "MONTH",
    "주": "WEEK", "주일": "WEEK", "week": "WEEK", "weeks": "WEEK",
    "일": "DAY", "day": "DAY", "days": "DAY",
    "시간": "HOUR", "hour": "HOUR", "hours": "HOUR",
    "분": "MINUTE", "minute": "MINUTE", "minutes": "MINUTE",
    "초": "SECOND", "second": "SECOND", "seconds": "SECOND",
    "퍼센트": "PERCENT", "프로": "PERCENT", "%": "PERCENT", "percent": "PERCENT",
    "배": "TIMES",
    "원": "KRW", "won": "KRW",
    "달러": "USD", "dollar": "USD", "dollars": "USD",
    "엔": "JPY", "yen": "JPY",
    "유로": "EUR", "euro": "EUR",
}
"""Every spelling read as a unit, mapped to its canonical unit.

One table serves both sides of the comparison -- the counter a question asks in
and the unit a value states -- so a row added for one side is live on the other,
and a test whose asked counter is the row under test cannot pin that row: delete
the row and the question side goes silent too.

Absent on purpose, each for its own reason:

* bare `월`. `3월` is March, and `개월` is the counter for a month-count and is
  present. Nothing in THIS table reads `월` as a month of the year either: the
  row is absent, so a `6월` that really did mean six months states no unit this
  rule can see, and is silent for want of any reading rather than by a
  judgement between two. `_TIME_POINT` does read digits run into `월`, but as a
  month term inside a longer shape rather than as a unit, so the two do not
  disagree.
  The exclusion is live on the suppression scan too, which is what makes
  `6월 및 30주` asked in months name the weeks. Admitting `월` there was measured
  against #451 and declined: it silences `3월 15일간`, `3월 내 15일 소요`,
  `3월 계약 15일 소요` and their siblings, values whose duration other tests
  assert is caveated, and it reaches the `N월 ..., N주` shape at large, where
  the `3월` is the month of the year far more often than a count of them.
  The reach is the DIGIT month only -- `전월 대비 3일 단축` and `매월 15일간`
  keep their caveats, since the scan needs a digit in front of the `월` --
  which narrows the cost without
  changing the verdict. Declining a `월` that falls inside a `_TIME_POINT` span
  does not rescue the narrower cases either: `2년 3월` and `10000년 3월` match no
  branch of that pattern.
* `일` is present, unlike `월`: `3일` is three days far more often than it is
  the third of the month. That is a judgement about which reading is commoner,
  not a guarantee that the other one is caught. What catches the other reading
  is `_TIME_POINT`, which needs a month term in front of the day, in the sense
  that pattern defines: `3월 15일`, `3월 중 15일` and `매월 15일` are dates.
  `15일 마감` has no month term at all and does state `일`. See
  `korean_measure_unit_mismatch`.
* `개년`. It fired on `5개년 계획`, which is the name of a plan rather than a
  duration.
  Live on the suppression scan too, so `5개년 계획 3주` asked in years names the
  weeks -- a wrong sentence #451 records. Fixing it means reading `5개년` as
  possibly stating years after all, which is the judgement this row was excluded
  for, so the two cannot both stand.
* `$`, `₩`, `€`. Every quantity here begins at a digit, and these precede their
  number, so no row in this table could reach them. `%` is read because it
  follows the number. That asymmetry is by construction; it is not an omission
  a row would restore.
  What #451 adds is not a reason to restore one but a name for what the
  omission costs. A symbol-led sum states no unit on either scan, so where a
  readable same-family unit stands beside it the caveat names that one:
  `₩20,000,000 (15,000달러)` asked in won reports `달러`, before this change and
  after. That is the general form `korean_measure_unit_mismatch`'s last bullet
  states -- an accepted silence with a readable neighbour is a latent wrong
  sentence -- reached by a cause that is neither the number nor a missing row,
  but the side the symbol sits on.
"""

_UNIT_SUFFIX_MEMBERS = ("간", "가량", "정도", "쯤", "짜리")
"""The particles that may follow a quantity and leave it readable as one.

Read by three things now, so the property it claims matters: `_UNIT_SUFFIX`
builds the value patterns from all of it, and `_TIME_POINT`'s day branch reads a
subset (`_DAY_DURATION_SUFFIXES`) for the converse property, which two members
do not have. What this tuple asserts is only the first direction -- a member
leaves a quantity readable -- and borrowing it for the second is the mistake
that partition exists to record.

Every member is Hangul, and `_VALUE_MEASUREMENT_RELAXED` is dropped from that
pattern on exactly that ground. A test that restated the members as its own
literal would go on passing if a non-Hangul one were added here, which is the
case the argument does not survive: a digit member makes `3년2주` read as `년`
alone and a `몇 주인가?` against it answer "the verified value states 주".
"""

_DAY_DURATION_SUFFIXES = ("간", "가량", "짜리")
"""The suffixes whose presence proves an `N일` is a duration.

A subset of `_UNIT_SUFFIX_MEMBERS`; `_TIME_POINT`'s day branch consults this and
not that. The criterion is not "does it mean approximately" -- `가량` does, and
is here -- but whether the suffix attaches to a point in time as readily as to a
quantity. The test is whether `3시X` is ordinary Korean: `3시쯤`, `3시 정도` and
`3시께` are, `3시가량` is not.

That is a lexical judgement about Korean, stated so it can be argued with rather
than proved from anything here. What the file does supply is `3월 15일경`, where
the standard date approximator has never been listed and the value reads as a
date. Reading the same split off the meanings arrives at it independently: `간`
is "for the duration of", `짜리` is "worth of", and `가량` approximates a
quantity rather than a point.

Both halves are literal, and neither is derived from the other. An open class
needs two things, and deriving one half gives only one of them: a safe default,
so an unclassified member costs a caveat rather than inventing one, and a
tripwire that refuses to take that default silently. The default here is safety
-- a suffix in neither tuple is not consulted, so the day stays a date -- and
the tripwire is the union equality the tests assert, which fails and names the
decision someone has to make. Derive either half and that equality holds by
construction: `께` would join `_UNIT_SUFFIX_MEMBERS`, land on a side by
arithmetic, and say nothing. Under exclusion it landed on this one, which made
`3월 15일께` a wrong sentence.
"""

_DATE_APPROXIMATOR_SUFFIXES = ("쯤", "정도")
"""The `_UNIT_SUFFIX_MEMBERS` that approximate a point in time as readily as a
quantity, so their presence proves nothing about the day they follow.

Literal rather than the complement of `_DAY_DURATION_SUFFIXES`, for the reason
given there.
"""

_UNIT_SUFFIX = r"(?:" + "|".join(_UNIT_SUFFIX_MEMBERS) + r")?"
"""The particles that may follow a unit and still leave it read as one.

`2년간` states two years. The set is closed, and the lookahead in
`_VALUE_MEASUREMENT` still applies after it, so `3일간의 일정` and `3주간격`
state no unit here: with the suffix taken, the next character is Hangul and the
lookahead refuses it; without it, `간` is Hangul and the lookahead refuses that.
"""

_VALUE_MEASUREMENT = re.compile(
    r"[0-9][0-9,.]*\s*[만억천조]?\s*(?P<unit>"
    + "|".join(re.escape(s) for s in _MEASUREMENT_UNIT_SPELLINGS)
    + r")" + _UNIT_SUFFIX + r"(?![가-힣0-9A-Za-z])"
)
"""One quantity stated inside a value: ASCII digits, at most one of four Korean
magnitude words, and a unit spelling.

`[만억천조]?` is one character, not a run, so `3만원` is read and `2천만원` is
not; it is those four and no others, so `2백만원` is not either; and the digits
are `[0-9]` rather than `\\d`, so `３년` is not. All three of those bounds are
narrower than `_VALUE_MEASUREMENT_RELAXED`'s, which is a statement about the
NUMBER and not about the two patterns as wholes: since #453 the relaxed one
carries a refusal of its own in `_UNIT_SHADOW_GUARD`, one this pattern already
makes through its trailing lookahead. The asymmetry has a direction. This
pattern decides what a value STATES and its output is put in front of a reader,
so widening it ADDS sentences and needs a sweep of its own; the relaxed one
decides only whether to stay silent. #451 widened the relaxed number and left
this one where it was, which is why those three are silent here and suppress
there.

Requiring the digits is the whole precision of this rule. Ordinary Korean prose
is full of syllables that are also unit spellings -- `지원`, `내년`, `일정`,
`분야` -- and read without a number in front of them they turn a caveat into
noise. The prose sweep in the tests is what measures that.

The alternation is built from the spellings table in the table's own order, and
what reads `3달러` as USD is the trailing lookahead plus backtracking rather
than that order: `달` is tried first and matches, the lookahead rejects it
because `러` is Hangul and so inside the lookahead's class, and the engine
backtracks into `달러`. Every prefix pair in the table today is extended by a
Hangul or Latin character, both of which that class covers, which is why
re-sorting the table by length changes no reading. A spelling extended by
punctuation would sit outside the class, and there the order would decide.
"""

_MONTH_WORD_MEMBERS = (
    "매월", "매달", "금월", "익월", "내월", "당월", "전월", "차월",
    "다음 달", "이번 달", "지난 달", "내달",
)
"""The month terms this file recognises as words rather than digits.

Like `_UNIT_SUFFIX_MEMBERS`, this exists as a tuple so a test can assert over
the live set instead of over a copy of it. `_TIME_POINT` states what a month
term does; this only lists the word forms of one.

The list is the ones that have been found, not the ones that exist -- Korean
has more ways to name a month than any closed tuple holds, and a word outside
it puts its value back in `korean_measure_unit_mismatch`'s residue rather than
into any error. `금월`, `내월` and `내달` were added after the first draft
shipped without them.

A word list fails in two directions and only one of them is above. The other is
a member matching inside a longer word, which is why the alternatives carry a
left bound -- see `_MONTH_OF_YEAR`. That bound is on digits only, so the digit
spelling `1차월` is excluded and the Sino-Korean numeral spelling `일차월` is
not; a member that is a common word tail would need more than a bound.
"""

_MONTH_PART_MEMBERS = ("의", "중", "초", "말")
"""The parts that may stand between a month term and its day, named for the tests.

Closed on purpose -- see `_TIME_POINT`, which is where the rule lives.
"""

_MONTH_OF_YEAR = (
    r"(?:[0-9]{1,2}\s*월|"
    # The word alternatives carry a left bound the digit one must not have.
    # Without it `차월` matches inside `1차월`, which is month one of a
    # programme and not a point in time at all, and `1차월 3일 소요` lost a
    # caveat it had earned. Digits are the only thing excluded, so `해당월` and
    # `익익월` keep matching on their tails -- an accident, but one that lands
    # on the right answer, and a Hangul bound would give it up.
    + r"(?<![0-9])(?:"
    # Every member is Hangul plus at most one space, so they join raw -- the
    # premise `_UNIT_SUFFIX` joins on -- and the space is relaxed so
    # `다음달 1일` reads like `다음 달 1일`.
    + "|".join(w.replace(" ", r"\s*") for w in _MONTH_WORD_MEMBERS)
    + r"))"
)

_TIME_POINT = re.compile(
    # A year in front of the month term belongs to the same date, so the span
    # reaches back over it. No left bound here, unlike the year+month branch --
    # see the docstring, where the two bounds are told apart.
    r"(?:[0-9]{2,4}\s*년\s*)?"
    + _MONTH_OF_YEAR
    + r"\s*(?:" + "|".join(_MONTH_PART_MEMBERS) + r")?\s*[0-9]{1,2}\s*일"
    # Not a day of the month if what follows proves it is a duration. That is
    # `_DAY_DURATION_SUFFIXES`, a subset of `_UNIT_SUFFIX_MEMBERS` and not that
    # tuple -- see the docstring for why the two differ.
    + r"(?!(?:" + "|".join(_DAY_DURATION_SUFFIXES) + r"))"
    # The minute and the second belong to the same clock time, and each is
    # optional independently of the other -- nested, `3시 20초` left its `초`
    # outside. Neither carries a lookahead of its own; the docstring weighs the
    # two candidates for one.
    r"|[0-9]{1,2}\s*시(?![가-힣])(?:\s*[0-9]{1,2}\s*분)?(?:\s*[0-9]{1,2}\s*초)?"
    r"|['’‘]\s*[0-9]{2}\s*년"
    r"|(?<![0-9])[0-9]{2,4}\s*년\s*[0-9]{1,2}\s*월"
    r"|(?<![0-9])[0-9]{4}\s*년(?![0-9])"
    r"|(?<![0-9])[0-9]{2,4}\s*[-./]\s*[0-9]{1,2}\s*[-./]\s*[0-9]{1,2}"
)
"""Shapes that make a value a point in time rather than a quantity of one.

`2021년` is a year, not two thousand and twenty-one years, and a question
asking `몇 개월인가?` must not be told that value states years.

The guard is span-local: `_value_measure_units` drops the quantities that
overlap a match of this pattern and reports the rest, so a duration standing
outside every match is still reported. `2021년 착수, 총 3주` and
`2021년 기준 30분` each state a real same-family mismatch and each now names it;
so do `3시 시작, 3주 소요`, `2024/01/02 3주`, `2021-03-15 (3일)` and
`12.5.3 버전, 3주 소요`. #452 is where the whole-value rule that lost all of them
is recorded, and #450 is where the class it lost had last been widened.

Overlap has two near neighbours and is neither of them, and each difference has
a witness. Masking the span out of the string would let `_VALUE_MEASUREMENT`'s
trailing lookahead see the fill character instead of the real neighbour, so
`3주2021-03-15` would be read as stating weeks -- the lookahead refuses `3주`
there because a digit follows it, and that refusal is the whole precision the
fill would spend. Dropping the overlapping match leaves every character where it
was.

Containment -- dropping only the quantities a span fully covers -- is the nearer
neighbour, and the one a later simplification reaches, since it reads as a tidier
way of writing the same rule. It is not the same rule, because a quantity can
cross a span's boundary in either direction and containment keeps every one that
does. In `2021-03-15일` the span is (0, 10) and `_VALUE_MEASUREMENT` reads `15일`
at (8, 11), crossing the right edge; containment keeps it and reports the value
as stating days, the one reading the ISO branch exists to refuse. In
`10000년 3월 15일` it is the left edge: the year prefix takes no left bound, so
the span opens at the inner `0000년` at (1, 13) while the real `10000년` runs
(0, 6), and containment calls ten thousand years a duration. The arguments below
that rest on the difference are not only the ISO one, and they are not a fixed
list either -- any passage whose value is silent because a quantity STRADDLES a
span is one, since containment would report that quantity. A quantity sitting
wholly inside a span is dropped by both rules, so `매월 15일`, `3시 30분` and
`2021년` rest on nothing here. Those found so far:
`3월 15일쯤` and `매월 15일정도` for the day approximators, `3시 30분간` and its
siblings for the swallowed clock tail, the two long years for the prefix's
missing left bound, and `12.5.33주` and `2021-03-153일` for the flush-quantity
residue two paragraphs down. The clock tail and the long years are visible only
under this widened pattern, so a probe run against the pre-#452 one
under-reports the class.
`test_a_quantity_is_dropped_by_overlap_and_not_by_masking_or_containment`
recomputes both neighbours and asserts each differs.

What a span COVERS therefore matters in a way it did not before. Under the
whole-value rule a branch only had to prove the value held a point in time; now
a component left outside the span is read as a quantity beside it, so each
branch has to run from the coarsest component of its expression to the finest.
Two branches below take a component for that reason alone and for no other: the
day branch takes an optional year, and the clock branch takes an optional minute
and second.

The set of components that can cost anything is closed, and closed by something
other than a word list -- it is the calendar and the clock, year, month, day,
hour, minute, second, which is not a class the next input extends. Narrower
still: a component can only cost a caveat if it can be read as a unit, and
`_MEASUREMENT_UNIT_SPELLINGS` decides that. `월` and `시` are absent from that
table on purpose, each for a reason given beside it, so no value can be said to
state them and no span has to reach them. `_VALUE_MEASUREMENT_RELAXED` draws
from the same table and feeds only `_value_states_asked_unit`, whose one output
is suppression, so what it decides is whether a caveat is suppressed and never
which unit a caveat names, and it does not widen the set. Since #453 it can
START a caveat, by refusing a spelling it used to read, but the one it starts
still names a unit `_value_measure_units` reported, so the set is untouched
either way and this argument is unaffected.

The premise is checkable on both of its sides, and
`test_a_span_covers_every_component_a_value_could_be_said_to_state` checks them
separately, because only one of them is live in the same sense. It recomputes
the intersection from `_MEASUREMENT_UNIT_SPELLINGS`, so adding `월` or `시` to
that table fails it for want of a fixture. There is no list of components to
recompute the other side from, so that side is pinned from the pattern instead:
the unit spellings occurring literally in `_TIME_POINT.pattern` are `년`, `달`,
`분`, `일` and `초`, and a branch naming a sixth -- `주`, say, for a week of the
month -- fails the test. `달` is on that list only through the members of
`_MONTH_WORD_MEMBERS` that spell it, each of which puts a Hangul syllable in
front of it with at most a space between, so a digit run can never reach a `달`
inside a span and no value can be said to state months there. The test derives
that from the live tuple rather than taking this sentence for it.

Say the rule as "the quantities overlapping a span are dropped" rather than as
"a guarded value is not read", because the value IS still read:
`_value_states_asked_unit` does not consult this guard, so
`_value_states_asked_unit("매월 15일", "DAY")` is True while
`_value_measure_units("매월 15일")` is empty. What ends a caveat is the reported
list, whatever the suppression scan sees. That is the same two-scan asymmetry
`korean_measure_unit_mismatch` describes.

What span-local costs is the accidental cover the whole-value rule was giving
to every quantity standing outside a span. That is a total statement rather
than a list: the guard used to empty the reported list whenever it matched
anywhere, so every wrong sentence `korean_measure_unit_mismatch` records was
silenced whenever some other part of the value happened to be a point in
time, and span-local withdraws that from all of them at once.
`2021년 계약, 15일 마감` is now told it states `일`, `2021년 착수, 21년` that
it states `년`, `2021년 계약, 2 second review` that it states `second`,
`2021년 기준, 6월 및 30주` that it states `주`, and
`2021년 계약, 15,000달러` that it states `달러` -- one witness per
entry would just be the list of entries again. None of them is a new
misreading: each value reads the same way standing alone and has since before
#450. What changed is the reach, and it changed for the whole list rather
than for the members someone thought to name.

What is total is the WITHDRAWAL, not the outcome, and the two must not be read
as one. Whether a caveat then names the entry is decided after this guard has
run, by `korean_measure_unit_mismatch`, and the account below is read off that
function's three ways of returning nothing rather than collected from examples.
It returns nothing when the question names no unit at all; when the suppression
scan finds the asked unit in the head's own notation; and when its loop finds
nothing to name. That last one is the one with several roads into it: the
entry's quantity may be covered by a span, or never read at all --
`_VALUE_MEASUREMENT`'s trailing lookahead is `(?![가-힣0-9A-Za-z])`, so a unit
run into Hangul, a digit or Latin is refused, and the `15일` in
`3월 15일과 20일` is never read for that reason, as is the one in
`매월 15일동안 3주` -- or read, and outside every span, and passed over anyway
for measuring something else, which is what becomes of a `15일` when the
question asks in `몇 원인가?`. A fourth thing decides not whether but WHICH:
only the first same-family quantity is returned, so an earlier one outranks the
entry's. Those are the exits, so a mechanism not among them would have to live
somewhere other than this function.

The outcome therefore turns on the question as much as on the head, and the two
witnesses differ in exactly that way. `3시 5분` is silent whatever it is asked,
because the `5분` is the clock's own minute and sits inside the span -- this
guard working, not failing, under every question the rule can put.
`2021년, 100주` is silent asked in `몇 년인가?`, where `_value_states_asked_unit`
reads the head's `2021년` as years and returns the answer it gave before this
change; asked in `몇 개월인가?` the same value now states `주` where it was
silent before, so it is no counter-example to the withdrawal being total.
`2021년 계약, 3주 소요, 15일 마감` is the third: its `15일` IS reported and still
never reached, because the three weeks stand in front of it. So "every entry
loses the cover" is exact, and "every entry is caveated now" would be false.

Two of them are argued separately below because their prospects differ, not
because the cost stops at two. #460's residue -- a bare `N일`, a bare
two-digit year -- is where nothing is left in the value to read. The elided
second member of a date is where something is.

The reach is widest where the head of a second date is elided, and that is
ordinary Korean rather than a corner. `3월 15일~20일`,
`매월 15일, 30일` and `3월 15일 및 20일` each name two days of one month; the
branch takes the first and the second stands outside every span, so each is now
told it states `일`. `3시 30분 ~ 45분` is the same shape on the clock and
`2021년, 22년` on the year. This one is NOT #460's residue, because a month term
IS left in the value to read -- but nothing measured so far reads it whole. A
bounded continuation on the day reaches the tilde spelling and none of the
hyphen, comma, `·`, `및`, `과` or `/` ones, and the separator set is an open
lexical class of the kind the day branch's own suffix rule exists to refuse.
Suppressing a leaked quantity whose unit a span already states covers every
separator, but only where the head's own component survives
`_VALUE_MEASUREMENT`'s lookahead, so it would fix `3월 15일, 20일` and not
`3월 15일과 20일`, one particle apart. `korean_measure_unit_mismatch` carries the
class and the tests record it.

Every alternative is needed by some value, and so is every member of the two
tuples: delete any one of them and some value changes its answer, which is what
the tests beside this file are built to catch. No count is quoted here -- the
pattern itself is the list.

A day of the month is a month term, an optional month part, and the day. The
month term is one or two digits run into `월`, or one of `_MONTH_WORD_MEMBERS`.
`개월` cannot be reached, because the digits must run straight into `월` with
only whitespace between -- the same property that keeps `12년 6개월` a duration
one branch below. This replaced a branch that required the day to stand
immediately beside a digit month, and with the part group empty it reads
exactly what that branch read, so no value that was a date stops being one.
`3월 15일`, `3월 중 15일`, `3월의 15일` and `매월 15일` are now read alike.

A year in front of the month term is part of the same date, and the branch takes
it as an optional prefix so that the span reaches back over it. Without the
prefix, `2021년 3월 15일` is matched by the year+month branch, which stops at
`3월` and leaves a `15일` outside the span to be read as fifteen days -- the day
branch cannot take it instead, because it must begin at the month term and the
earlier match has already consumed past it. The prefix is `[0-9]{2,4}` for the
reason the year+month branch's year is, and the two are written out separately
rather than shared because their bounds differ, which is two paragraphs down.

No left bound is placed on the number, and that is deliberate rather than an
omission: the branch this replaced had none, so `123월 15일` matches on its
inner `23월 15일` and is silent, and adding a bound would make that value newly
caveated -- a caveat gained, which this rule may not do quietly. The word
alternatives are bounded, for the opposite reason given beside them. One
consequence of the digit month being unbounded is that the width it admits is
decoration: `[0-9]{1,2}` and `[0-9]` and `[0-9]{1,3}` all read the same values,
because a longer run simply matches further in. The same is true of the clock
hour. Only the DAY's width is load-bearing, since the day must start where the
month term ended.

The year prefix takes no left bound, and that is the bound the year+month branch
does take. The two are doing different work. There the year alone is what makes
the value a date, so a spurious inner match silences a genuine duration and
`10000년` must not read as `0000년`. Here the month and the day have already
decided the value is a date; the prefix only chooses how much of it the span
covers, and a longer span can only remove a caveat. Bounded, `10000년 3월 15일`
and `12021년 3월 15일` have a leading digit left over and are told they state
years. A one-digit year is still left outside, so `1년 3월 15일` and
`2년 3월 15일` have their `1년` and `2년` read as durations and ARE caveated --
the same judgement the year+month branch makes on `2년 3월`, and the only place
a component that could belong to the date's own notation is deliberately left
out of the span.

The day refuses a `_DAY_DURATION_SUFFIXES` tail, for the reason the clock hour
refuses a Hangul one. `_UNIT_SUFFIX` makes `15일간` fifteen days, `15일가량`
about fifteen days and `15일짜리` a fifteen-day one, so a day wearing any of
those cannot be the fifteenth of anything; without the lookahead the branch read
the `15일` inside and called `매월 15일간` a point in time, losing a caveat on a
value with no point in time in it at all, which is not a loss this guard's
bargain covers.

What the branch consults is bounded morphologically, and that is the boundary
worth stating because it is the one that closes. "Means a duration" is an open
lexical class -- `간`, `가량`, `짜리`, then `동안`, `남짓`, `내내`, `이상`,
and every free word that implies a span -- so enumerating it would narrow one
shape and leave its neighbours, the failure #450 was opened against. "Is bound
to the number" is closed, and that is all the argument needs: every candidate is a
member of `_UNIT_SUFFIX_MEMBERS`, which is enumerated, so nothing outside it can
be consulted. So the rule is consult only what is flush against `일`, and among
those not one that approximates a point in time as readily as a quantity. A
free word after a space is not consulted, and `korean_measure_unit_mismatch`
discloses what that costs.

`_DAY_DURATION_SUFFIXES` is bound and closed, but it is NOT the set of bound
suffixes, and reading it as one is the mistake this passage has made more than
once. Two filters stand between them, and each has a member to its name: `정도`
is dropped for not being bound at all, a 명사 whose standard spelling is spaced;
`쯤` is bound, written flush, and dropped anyway for approximating a point in
time. Either filter can take any member, so read the tuple as what survived both
rather than as a class -- the identity is what keeps going false as members move
between the two halves.

A space therefore decides the verdict for a bound suffix, and that is the
boundary rather than the defect `_VALUE_MEASUREMENT_RELAXED` exists to fix. There
`3시간30분` and `3시간 30분` are both standard and got different answers; here
`15일간` is standard and `15일 간` is a misspelling. Where the free form is the
standard one there is no flip: `정도` is a noun, and `매월 15일정도` and
`매월 15일 정도` are both silent.

`_DAY_DURATION_SUFFIXES` is a subset of `_UNIT_SUFFIX_MEMBERS`, and that is the
second half of the rule. `_UNIT_SUFFIX` is the set of particles that leave a
quantity readable as one -- the only property its own docstring claims. The day
branch needs the converse, that the particle proves its `N일` is not a date, and
`쯤` and `정도` do not have it: an approximated date is still a date. `3월 15일경`
is the same idea wearing a suffix this file has never listed, and it reads as a
date. Taking `쯤` would have given two spellings of one meaning two answers, and
it un-fixed this issue's own headline -- `매월 15일` a date and `매월 15일쯤` a
duration, one particle apart.

This is the one place #450 made a value newly caveated rather than newly silent,
and it did so knowingly: `3월 15일간` was silent before #450 too, so
the lookahead reaches back past #450's own additions. Which values gain
depends on the baseline, and both readings are true of different ones: against
this pattern with the lookahead removed, any month term can gain; against
`e7ac2a7`, only a digit month can, because the older guard had no word months to
silence and so nothing to stop silencing. The tests derive each from its own
baseline rather than restating either here. Every gained value states days, so
each added caveat is right, and the "may not do quietly" above is the standard
being met rather than evaded.

`_MONTH_PART_MEMBERS` is closed, and closing it is the whole precision of the
branch. Admitting any single Hangul syllable instead also takes
`3월 내 15일 소요`, `3월 후 15일 소요`, `매월 약 3일` and `전월 대비 3일 단축`,
each of which states a real duration. What the closed set costs is
`3월 중 15일 소요` and `3월 말 15일 소요`, honestly ambiguous and read here as
the fifteenth.

A clock hour is a point in time and `시간` is a duration, and the tables are
what separate them: `시` is in neither `_MEASUREMENT_UNIT_SPELLINGS` nor
`_KOREAN_MEASURE_COUNTER` while `시간` is in both, so digits running into `시`
state no unit this file can read and can only be a clock. The lookahead is
`(?![가-힣])` rather than `(?!간)` because `3시그마` and `5시리즈` are words,
not times; it is not `(?![가-힣0-9])` because `3시30분` is half past three. A
clock time with a Hangul tail -- `3시부터`, `3시경`, `3시반` -- falls outside
and needs nothing, since it states no unit for a caveat to be wrong about.

The minute and the second belong to the same clock time, so the branch takes
them and the span covers them. `3시 30분` is half past three, and with the span
ending at `3시` the `30분` outside it reads as thirty minutes, which asked in
`몇 시간` is a wrong sentence. The two tails are optional and INDEPENDENT, so
`3시`, `3시 30분`, `3시 20초` and `3시 30분 20초` all match. Nested, the second
could only follow a minute, and `3시 20초` -- an hour and a second with no
minute between them -- left its `초` outside the span to be read as twenty
seconds.

The two silences that buys are not equally comfortable, and the second should
not be read as the first's twin. `3시 30분` is how a clock time is written, so
silencing `3시 30분 소요` costs a reading few would have wanted. `3시 20초` is
not: nobody writes a time as an hour and a second with the minute left out, so
on that shape the duration reading is the likelier of the two and
`3시 20초 소요` is the more plausible loss. It is still the right trade, because
the alternative was not a silence but a definite wrong sentence -- `3시 20초`
told it states seconds -- and this rule prefers a missing caveat to a false
one. Recorded as accepted rather than as costless.

That the un-nesting can only cost silences is a property of the shape rather
than a corpus result, and the difference matters to whoever re-measures it.
Both tails are optional groups, so removing the nesting can only make a match
longer, never shorter or absent; a longer span can only drop more of what
`_VALUE_MEASUREMENT` found, and dropping more can only remove a reported unit.
So no input exists on which this change adds a caveat -- a measurement showing
"0 gained" is confirming the shape, not sampling for it. Read a future 0 that
way, rather than as evidence that the corpus was wide enough.

Only whitespace joins the components,
and that is what keeps the branch honest: `3시, 30분 소요` ends the clock at the
comma and the thirty minutes beside it are read as the duration they are.

Neither tail carries a lookahead of its own, and two candidates were weighed
rather than one. `(?![가-힣])`, the clock hour's own, costs a wrong sentence:
`3시 30분쯤` then leaves `30분쯤` outside the span, `_UNIT_SUFFIX` reads it as
thirty minutes, and around half past three is reported as a duration.
`(?!(?:간|가량|짜리))`, the day's, costs something too, and the cost is a class
rather than a count: every clock time whose tail wears one of those three
suffixes, on the minute or on the second, is read differently wherever it
appears -- what that then costs is a second question, answered below. Both
tails means both positions the second can take -- `3시 30분간`, `3시 30분가량`
and `3시 30분짜리` on the minute, `3시 30분 20초간` and its two on a second
behind a minute, and `3시 20초간` and its two on a second directly behind the
hour, which only became reachable when the tails stopped being nested.

What it does to them splits in two. Standing alone they gain a caveat where
they were silent, which this guard's bargain allows. Standing BEFORE another
duration they gain nothing -- they take the caveat that is already there and
move it onto the clock's tail, so `3시 30분간 3주` reports `('개월','주')` today
and would report `('개월','분')`, naming the swallowed tail instead of the three
weeks the value is about, and `3시 20초간 3주` would report `('개월','초')` the
same way. Order decides which of the two happens, and not presence:
`korean_measure_unit_mismatch` reports the FIRST same-family unit it finds, so
putting the other duration first moves nothing at all -- `3주 후 3시 30분간`
answers `('개월','주')` either way.

So it is declined on the reading and on that: those tails are likelier to be a
mangled `3시간 30분X` than a duration bound to a minute already inside a clock
time, and the day's tuple was chosen by asking whether `3시X` is ordinary
Korean -- a question about the hour, not about a minute behind one. A swallowed
tail is an accepted silence of the kind this guard already makes, and the day
needs its lookahead for the different reason that `매월 15일간` holds no point in
time at all.

`'21년` is caught where bare `21년` is not, and the apostrophe is the whole of
the difference: it stands in for the elided century and no duration is written
with one. Three characters are read: the straight `'`, the curly `’` that is
the apostrophe proper, and the opening `‘` that a word processor autocorrects a
leading straight quote into -- which is how the character usually arrives.

A year followed by a month is a date whatever the year's width, which is the
branch that reads `21년 3월` and `25년 12월` as dates rather than as twenty-one
and twenty-five years. Two-digit years are ordinary Korean document notation, so
without it the issue's own headline question -- `기간` asked in `몇 개월` --
answers a date range with "the verified value states 년". The year is bounded
below at two digits: `2년 3월` is left alone because one digit is as likely to be
a duration as a date and nothing here separates them. What distinguishes this
branch from a real duration is the counter, not the number: `12년 6개월` is
twelve years and six months and stays a duration, because `개월` is not `월`.

A bare two-digit year is deliberately NOT caught. `21년` on its own really can be
twenty-one years, so it is left reading YEAR and disclosed in
`korean_measure_unit_mismatch` instead. Widening the four-digit branch to
`[0-9]{2,4}` would silence it, and that is the trade this declines. Written with
an apostrophe it is caught, for the reason given above; bare, it is not.

The four-digit year branch is bounded on both sides, and the two bounds do
different work. The left-hand `(?<![0-9])` is what stops a genuine `10000년`
matching on its inner `0000년`. The right-hand `(?![0-9])` stops a four-digit run
that continues into more digits from being read as a year, which only a
contrived value reaches (`2021년12개월`).

The ISO branch earns its place narrowly. An ISO date on its own states no unit,
so the branch does nothing for `2021-03-15`; what it catches is a date with a
unit spelling run onto the end of it, `2021.03.15 일` and `2021-03-15일`, which
without it are read as stating days. It used to be the worst-paying of the four,
because the whole-value rule spent its match on the rest of the value as well
and `2024/01/02 3주` and `2021-03-15 (3일)` went silent with it. Span-local ends
that half of the cost: the quantity overlapping the date is dropped and a
duration standing clear of it is reported, so both are caveated again.

What is left is not the guard's doing but the branch's own reading, and it is
worth stating rather than counting as ended. A quantity written flush onto the
end of an ISO date has its digit run begin inside the date -- `2021-03-153일` is
read by `_VALUE_MEASUREMENT` as `153일`, starting at the date's own `15` -- so
the quantity overlaps the span, straddling its right edge rather than sitting
inside it, and overlap is what drops it. `2021-03-153일`,
`2024/01/023주` and `12.5.33주` say nothing. No reading reports those and still
keeps `2021-03-15일` off the days side, which is the whole of what the branch is
for; they were silent before this change too, so it removes that cost neither
more nor less than it removes any other.

Its year is `[0-9]{2,4}` for the same reason the year+month branch's is, and
holding it at four digits while arguing two-digit years are ordinary notation
one branch above was the contradiction that got it widened: `21.03.15일` and
`25-01-15일` are dates by exactly the premise this file already accepts. Bounded
below at two digits and on the left, so `2.03.15일` and `12021.03.15일` are not
dates. What the branch still misses is a date with no year at all -- `03/15일`
needs two separators to be reached and has one -- which is disclosed in
`korean_measure_unit_mismatch` rather than chased with an alternative of its
own. Reading a slashed month term was tried for #450 and withdrawn: it also
reads the numerator of a small-number rate, and `50/15일` stopped being read.

Widening the year also widened what else looks like a date: a dotted or dashed
numeric triple whose first component is two or three digits reads as one, so
`12.5.3`, `10.1.2` and `10.0.0.1` are points in time as far as this pattern is
concerned. That was a third cost under the whole-value rule, where it silenced
the duration standing beside the version -- `12.5.3 버전, 3주 소요` and
`10.1.2 릴리스, 3주` were caveated before the widening and not after. Span-local
takes the cost back rather than the reading: the triple still matches, and a
version number states no unit of its own, so a duration standing clear of it
-- `12.5.3 버전, 3주 소요`, `10.1.2 릴리스, 3주` -- is reported. Written flush
onto the end of the triple it is not: `12.5.33주` and `10.1.23주` have their
digit run begin at the version's own first digit, so the whole quantity
overlaps the span and is dropped. That is the residue the ISO paragraph above
already records, reached by the same mechanism, and not a second one. A one-digit first component
still falls outside the branch, and that bound no longer separates any of those
values -- what it still separates is `1.2.3 일`, which states days where
`12.5.3 일` does not, and the test re-derives that rather than taking this
sentence for it.

`1500년` and `2000년간` are read as calendar years; both spellings are really
used for durations too, so that reading is honestly ambiguous and this rule
picks the date one.
"""


_SINO_KOREAN_MAGNITUDES = "십백천만억조"
"""The magnitude words `_VALUE_MEASUREMENT_RELAXED`'s number may run through.

The Sino-Korean magnitudes as far as documents use them. This is TWO series
joined, and the join is worth naming because only one of them continues: the
sub-myriad steps 십 백 천, which are 10^1 to 10^3 and stop there because 10^4
has its own word, and the myriad steps 만 억 조, which are 10^4, 10^8 and 10^12
and go on to `경`. Both are enumerable with a defining property rather than
open lexical classes like `_MONTH_WORD_MEMBERS`, so naming them is one decision
rather than a list that grows each round. Reading the six as one run is what
makes the boundary hard to see: it invites looking for a next member after
`조` by the same step that got from `십` to `백`, when what actually continues
is the myriad half, at `경`. `경` is out because a sum of 10^16 has not been
seen in this data; that boundary is stated here so it can be argued with, and
`test_the_magnitude_class_is_a_series_and_this_is_where_it_stops` re-derives
what it costs rather than restating this sentence.

What it costs is narrow, and narrow for a structural reason: the run has to
reach from a digit to the unit without interruption, so it starts at the LAST
digit run and a number escapes when a magnitude outside the class stands
anywhere between that run and the unit. `1경5천조원` is read, because its last
digit run is the `5` and only `천` and `조` stand after it. `1경원`, `1천경원`
and `1억경원` are not, and the `천` in the second of those is in the class and
does not help -- what decides is the whole gap, not any one member of it.

`_DAY_DURATION_SUFFIXES` says an open-ended class needs a safe default and a
tripwire. Only the tripwire is available here, and that is worth saying plainly
because it inverts the usual argument in this file: on THIS scan an
unrecognised member does not cost a caveat, it leaves a wrong sentence
standing, since failing to read the asked unit is what #451 is. There is no
consolation in the default, so the tripwire carries the whole load.

`[만억천조]` was what shipped for #445 -- the myriad steps plus `천`, which is
neither a full series nor a closed one -- and `2백만원`, two million won and as
ordinary in a Korean document as `2천만원`, was told it states `달러` for
exactly that reason. Adding `십` and `백` is what makes the sub-myriad half
complete rather than partial, and completing it is the decision; `십` on its
own buys `5십원` and `2백5십원`, which
`test_the_magnitude_class_is_a_series_and_this_is_where_it_stops` pins so the
member cannot be dropped with the suite green.
"""

_RELAXED_QUANTITY_NUMBER = (
    r"\d[\d,.]*\s*(?:[" + _SINO_KOREAN_MAGNITUDES + r"]\s*)*"
)
"""The number `_VALUE_MEASUREMENT_RELAXED` reads, named so the tests can rebuild
the pattern instead of restating it.

Two of them opened with a copy of this text, and a copy of a pattern is a claim
about the pattern that nothing checks: both went on passing when the number
changed under them for #451, because their probes happened not to distinguish
the old shape from the new one. Naming it is the same remedy
`_UNIT_SUFFIX_MEMBERS` and `_MONTH_WORD_MEMBERS` get, and the probes were
widened at the same time.
"""

_UNIT_SHADOW_WORDS = ("분기", "주년", "년대", "주주", "secondary")
"""Words that a unit spelling only BEGINS, refused where they stand complete.

`분기` is a quarter and its first syllable is the spelling for MINUTE; `주년`,
`년대` and `주주` stand the same way to WEEK, YEAR and WEEK, and `secondary` to
SECOND. `_VALUE_MEASUREMENT_RELAXED` declines to read a spelling where one of
these stands complete in its place, which is what gives `3분기 실적, 2시간 소요`
asked in minutes its `시간` caveat back.

The class is open and takes both things `_DAY_DURATION_SUFFIXES` says an open
class needs. The DEFAULT is the safe direction: a word left off leaves the
reading this scan already made, so it costs a lost caveat rather than a new
sentence, which is what this scan did for every member here before the guard
existed. The TRIPWIRE is
`test_a_shadow_word_is_one_the_reporting_scan_already_refuses`, which refuses a
member whose complete form `_value_measure_units` still reads as a quantity.
`분간` is the shape it catches: with `일간` listed,
`korean_measure_unit_mismatch` answers a `몇 일인가?` about `3일간` with
`('일', '일')`, a caveat naming the asked unit against itself. No count is
written here; the tuple is the list.

Order inside the guard is free, unlike the `달러`-before-`달` constraint the
alternation carries, because a negative lookahead asks only whether SOME
alternative matches where it stands.
`test_a_word_a_unit_spelling_only_begins_is_not_that_unit` carries one fixture
per member and fails if a member is added without one.
"""

_UNIT_SHADOW_GUARD = (
    r"(?!(?:"
    + "|".join(re.escape(w) for w in _UNIT_SHADOW_WORDS)
    + r")(?![가-힣A-Za-z]))"
)
"""`_UNIT_SHADOW_WORDS` as a lookahead standing where the spelling would begin.

It consumes nothing, so every match `_VALUE_MEASUREMENT_RELAXED` makes still
begins at a decimal digit and `_value_states_asked_unit` reads it unchanged.

The inner `(?![가-힣A-Za-z])` is what makes the refusal conditional on the listed
word standing COMPLETE, and it is the whole of the guard's safety in the other
direction, since a listed word is also the prefix of longer strings that stand
in values which do state the asked unit -- `30분기준` is `30분` plus `기준`.
`_VALUE_MEASUREMENT_RELAXED` argues that and
`test_the_shadow_bound_admits_a_digit_and_refuses_a_letter` re-derives why the
class is not `_VALUE_MEASUREMENT`'s `[가-힣0-9A-Za-z]`.

Named rather than inlined because `test_a_unit_suffix_would_be_inert_here` and
`test_only_the_달러_before_달_constraint_decides_the_suppression_ordering` rebuild
this pattern, and a copy of a pattern is a claim about the pattern that nothing
checks -- which is what `_RELAXED_QUANTITY_NUMBER` was named for.
"""

_VALUE_MEASUREMENT_RELAXED = re.compile(
    _RELAXED_QUANTITY_NUMBER
    + _UNIT_SHADOW_GUARD
    + r"(?P<unit>"
    + "|".join(
        re.escape(s) for s in sorted(_MEASUREMENT_UNIT_SPELLINGS, key=len, reverse=True)
    )
    + r")"
    # No `_UNIT_SUFFIX` here, unlike `_VALUE_MEASUREMENT`. NOT because a match's
    # end is unread -- `finditer` resumes from it, so in general an optional
    # trailing group does change what is found later: `(?P<u>[ab])` reads "ab"
    # as a, b and `(?P<u>[ab])(?:b)?` reads it as a alone. It is inert here
    # because of what the two character sets are: every `_UNIT_SUFFIX_MEMBERS`
    # entry is Hangul, and every match must begin at a digit, so a start this
    # scan cares about can never fall inside a suffix that a longer match
    # swallowed. Ends do move; readings cannot. All three parts -- the premise
    # over the live tuple, the moving ends, the unchanged readings -- are
    # re-derived by `test_a_unit_suffix_would_be_inert_here`, not quoted here.
)
"""The suppression scan: `_VALUE_MEASUREMENT` read for whether a value CARRIES
the asked unit rather than for what it STATES.

How the two differ, and which way each difference runs, is set out in
`_value_states_asked_unit` and deliberately not here -- set out, and not counted
there either. A list in this line is what #453 falsified, by adding a part to
this pattern; the description belongs next to the code that has to be right
about it.

The lookahead is right for deciding what a value STATES -- `2년차` is a second
year of service, not two years -- but wrong for deciding whether the value
already carries the unit that was ASKED for. There it hid the asked unit and let
the caveat fire anyway: `3시간30분` answering `몇 시간인가?` was told the value
states minutes and that no conversion is applied, when the leading quantity is
exactly the hours asked for. A single space changed the outcome, because
`3시간 30분` passes the lookahead and `3시간30분` does not.

The number is wider in three ways, and #451 is what made them necessary. They
are three and not two, which matters below: the magnitude group is a RUN rather
than one character, its CLASS is `_SINO_KOREAN_MAGNITUDES` rather than
`[만억천조]`, and these are independent -- `2천만원` needs only the run, `2백원`
needs only the class, and `2백만원` needs both. So `2천만원`, `1억5천만원`,
`2백만원` and `2백원` are read -- `X천만원`, `X억Y천만원`, `X백만원` and `X백원`
are how a Korean document writes a sum, and `원` is a live question counter, so
a `몇 원인가?` answered any of them was told the value states `달러` beside an
answer whose leading figure is won. The digit class is `\\d`, every Unicode
decimal digit rather than a listed range, because naming a range would leave the
next script out; `verinote.text.nfc` is not `nfkc` and folding compatibility
forms would have this rule compare a value differently from every other
comparison made on it, but admitting the characters in a class local to this
pattern normalizes nothing, so the one-normalizer rule is not in play.

What is still out of reach is stated as a rule and not as a list, because a
list here has been wrong every time it has been written -- three times, each
time by leaving out a class the next reader found. The rule is about a unit
spelling and not about a value, and it is ONE existential condition with two
ways to fail:

    this scan reads a given unit ONLY WHERE SOME DECIMAL DIGIT stands
    before that unit's spelling with nothing between them but digits,
    separators, magnitude words from `_SINO_KOREAN_MAGNITUDES`, and
    whitespace.

Only where, and not wherever. The condition is what every match must satisfy,
so failing it at every digit is what puts a value out of reach -- which is the
whole of the account below, and that direction is exact: every match this
pattern makes begins at a decimal digit and reaches its spelling through those
four character classes and nothing else. It does not run the other way.
`3달러` has a digit before its `달` and an empty gap between them, and is still
not read as months, because the alternation takes `달러` first.
`test_only_the_달러_before_달_constraint_decides_the_suppression_ordering` is
where that constraint lives, and it is the one thing a reader cannot get from
the condition above.

That is a mechanism with a bound rather than a case that happened to be found,
and the bound is what keeps this from being a list. A prefix pair can only bite
when the two spellings CROSS CANONICAL UNITS, because the converse asks whether
the unit was read and not which spelling read it: `3주일` satisfies the
condition on its `주` and the alternation takes `주일`, and nothing is lost,
since both are WEEK. `달`/`달러` is the only cross-family prefix pair in the
table, and
`test_달러_is_the_only_prefix_pair_that_crosses_canonical_units` fails the day a
row creates a second one -- so a new spelling cannot quietly widen this
exception.

The other way it does not run is narrower and worth a clause rather than a
paragraph: a separator is admitted only inside the leading digit run and never
after a magnitude word, so `2천만,년` satisfies the condition as worded and is
read as nothing. `_RELAXED_QUANTITY_NUMBER` is the shape, and
`test_a_separator_is_admitted_only_inside_the_leading_digit_run` is the
tripwire: it was written because admitting `[,.]` after the magnitude group
left the whole suite green, so this exception was a reading of the constant
rather than a claim anything would notice losing. Both classes are held now,
which is what makes naming two of them a bound rather than a count.

SOME DIGIT and not EVERY DIGIT, and the difference is the whole sentence.
`1경5천조원` qualifies on its `5`, whose gap to the `원` is `천조`, and does not
qualify on its `1`, whose gap holds the `경`; the scan resumes from the next
start rather than giving up at the first, so the value is read. A reader who
takes the gap clause distributively gets that value wrong -- which is the
mistake an earlier draft of this paragraph made in the code, saying "the FIRST
digit" while a test in the same commit asserted the `5`. Existential is the
property; "reads from the last digit run" is a consequence of the run being
greedy, not the rule.

Say DIGIT and not just "some", because the quantifier has a second axis and
only one of them is this rule. Ranged over OCCURRENCES OF THE SPELLING it
would say every `원` in the value must be reachable, and that is false:
`2천만원 지원 (15,000달러)` holds two, of which only the first is, and the
value is read. `1경5천조원` cannot catch that mistake -- it has one `원` and
comes out true either way -- so the witness does not substitute for naming the
noun. `지원` is this file's own standing example for why the digit requirement
exists, which is how ordinary the wrong axis is.

Failing that condition at every digit puts a unit out of reach, and the groups
below are the ways it is failed. Read that direction only: the groups account
for what the condition excludes, not for everything this scan declines, since
the two classes above are declined while satisfying it. What the direction does
buy is the thing four drafts of this paragraph kept getting wrong -- a cause
that fails the condition needs no new entry here, because the condition already
covers it.

FAILING THE DIGIT. No decimal digit stands before the asked unit, so there is
nothing for the scan to start from. A Sino-Korean numeral (`이천만원`) is the
case this file has recorded longest, but the reachable ones are the native
Korean numerals -- `한 시간 30분` asked in hours, `두 달 3주`, `이틀 3주` --
and the quantities that carry no numeral at all: `반년`, `수개월`, `수십억원`,
`여러 달`. `반년` is why the condition cannot be phrased as "a numeral the scan
cannot spell": there is no numeral in it to fail to spell. `한 시간 30분` is
the most reachable of all of them, since `일 시간` is not Korean and an hour
and a half is ordinarily written that way -- though `1시간 30분` and `1.5시간`
are read, so what is out of reach is the notation and not the quantity.

FAILING THE GAP. Digits do stand before the unit, and EVERY one of them has
something in its gap: a magnitude outside the class (`1경원`, and `1천경원`,
whose in-class `천` does not save it -- there is no later digit to start from),
or an approximator (`3천만여원`, `20여년`). This is where the quantifier earns
its keep: one blocked digit proves nothing, since `1경5천조원` has a blocked
`1` and is read anyway. Position and not vocabulary -- the same `여` AFTER the
unit spelling never enters a gap, so `3개월여` and `2년여` read normally.

All of these are pre-existing rather than introduced by #451. That change
widened the gap condition, and it widened the digits themselves from `[0-9]`
to every Unicode decimal digit; what it left alone is the requirement that
there be a decimal digit at all, which is the whole of the first group.
`korean_measure_unit_mismatch` records what they cost.

The run takes no digits between its magnitude words, and that is measured rather
than assumed. `(?:[...]\\s*[\\d,.]*\\s*)*` reads `1억5천만원` from the `1` where
this reads it from the `5`, and both report KRW: a stacked number is a digit
run, then magnitude words possibly separated by further digit runs, then the
unit, so where there are inner runs this pattern starts at the last of them and
reaches the same unit. What that rests on is that no spelling in
`_MEASUREMENT_UNIT_SPELLINGS` begins with a magnitude character, so the region
the longer form swallows holds no unit to hide.
`test_the_magnitude_run_needs_no_inner_digits` re-derives the premise from the
live table and the equality from a corpus, and shows the two patterns really do
differ, since their match starts do.

Neither widening can put a unit in front of a reader, and that is a theorem
rather than a corpus result. This pattern's one caller is
`_value_states_asked_unit`, whose one output is the early return in
`korean_measure_unit_mismatch`, so the only way a wider number changes an answer
is by reading FEWER units. It cannot: the number's own language is
`[\\d,.\\s십백천만억조]` and no spelling begins with a character in it, so no
spelling can start inside a number and no number can extend across one, and at
every start where the narrower pattern matched this one matches the same span
and unit. `test_the_widened_number_can_only_silence` asserts that premise as
well as the outcome, so the argument degrades loudly rather than silently if the
table ever gains such a spelling.

Suppression can only ever produce silence, so reading generously here is
safety-increasing in a way that reading generously in `_value_measure_units`
would not be. What it spends is caveats, and it does spend them -- see the last
paragraph, which is part of the design rather than a caveat about it.

Dropping the lookahead alone would have been wrong for one specific reason: it
is what makes `3달러` read as USD rather than as `달` plus a stray syllable, so
without it a question asked in months would find `달` inside `달러` and suppress
a caveat the value never earned. `달`/`달러` is the only one of the table's ten
prefix pairs that crosses canonical units; the other nine are two spellings of
one unit, where which of them matches cannot change any answer. So the whole of
what the lookahead was doing for THIS scan is discharged by reading `달러`
before `달`.

The alternation is sorted longest spelling first because that states the
intent, but length is not the property to preserve -- putting `달러` before `달`
is. Reversed-table and reverse-alphabetical orderings also satisfy it and read
identically; table order, shortest-first and alphabetical do not and read
differently. No count is quoted here on purpose: the numbers in this file have
rotted once already, so
`test_only_the_달러_before_달_constraint_decides_the_suppression_ordering`
partitions the orderings off the live table and asserts both halves instead.
This is the opposite of `_VALUE_MEASUREMENT`, where the lookahead does the work
and the order is free.

What no ordering buys is a spelling that is only the head of an unrelated word,
and `_UNIT_SHADOW_WORDS` is the part of that this scan can settle. The rule is
necessity only:

    this scan reads a spelling as a unit ONLY WHERE the text there does not
    begin with a word in `_UNIT_SHADOW_WORDS` standing COMPLETE -- complete
    meaning no Hangul or Latin letter continues it.

`3분기 실적, 2시간 소요` asked in minutes is the witness: it named `시간` before
this scan existed, said nothing while the guard was absent, and names `시간`
again.

Complete is the whole of the second half, and dropping it inverts the rule's
sign, because a listed word is also the prefix of longer strings that a real
value writes: `30분기준` is `30분` plus `기준`, `10년대출` is `10년` plus `대출`,
`2주주기` is `2주` plus `주기`. Each has a listed word standing exactly where the
spelling does and each does state the asked unit, so an unbounded refusal takes
`30분기준 회의, 2시간 소요`, `10년대출 상환, 3개월 준비` and
`2주주기로 반복, 3개월` and turns a CORRECT SILENCE into a wrong sentence -- not
a lost caveat, since a value that states the asked unit had no caveat to lose.
That is the failure the boundary shapes weighed for this scan were rejected for,
arrived at from the other side, and it is the opposite direction from the one an
unlisted word costs.

The class is `[가-힣A-Za-z]` and not `_VALUE_MEASUREMENT`'s `[가-힣0-9A-Za-z]`,
and the difference is load-bearing rather than an oversight: a digit after a
listed word begins a new number, it does not continue a word.
`80년대2000년대 비교, 3개월` asked in years names its `개월` under this class and
is silent under the other, and the same goes for `3분기4분기 실적, 2시간 소요`.
The two lookaheads are asking different questions at different positions, and
`test_the_shadow_bound_admits_a_digit_and_refuses_a_letter` re-derives the cost
of merging them.

The list is open and takes both things `_DAY_DURATION_SUFFIXES` says an open
class needs, and the DEFAULT is what decides the shape. A word left off leaves
the reading this scan already made, which is a lost caveat and not a new
sentence, so an unlisted member costs nothing new; a boundary placed on the UNIT
instead has the opposite default, since josa is written flush against the noun
and `3주의 준비, 2개월 소요` would then be caveated against a value that does
state three weeks. The tripwire is
`test_a_shadow_word_is_one_the_reporting_scan_already_refuses`: a member whose
complete form the reporting scan still reads as a quantity puts the two scans on
different readings of one value, and `korean_measure_unit_mismatch` then answers
`('일', '일')`. No count is written here; the tuple is the list.

#451 widened the number and thereby widened every silence this scan already
makes; that is the cost of the change rather than a separate defect. What
reaches further is not a list of values but the three notations named above --
a magnitude run, a sub-myriad magnitude, and a non-ASCII decimal digit -- so
anything this scan already read wrongly it now reads wrongly in those too. The
guard above travels with them, because it is placed on the spelling and not on
the number: `２분기 실적, 2시간 소요`, `１주년 기념, 3개월 준비` and
`８０년대 후반, 3개월` are the full-width twins of the three Korean witnesses and
are caveated for the same reason they are. What stays silent is recorded one
notation at a time: `2천만주 보유, 3개월 준비` needs the run,
`3백주 보유, 3개월 준비` needs
the sub-myriad magnitude, and `１００주 보유, 3개월 준비` needs the digit class --
each silent under the shipped number and firing under exactly the one narrowing
that names it, which
`test_caveats_lost_to_the_suppression_scan_are_recorded_not_fixed` re-derives
rather than taking this sentence for. Those three are the `100주` shares
reading, which #467 carries, each held silent by a different part of the number
#451 widened rather than by how large it is. Notation and not quantity:
`20000000주`, `300주` and `100주` write those same three amounts in plain ASCII
digits and the pre-#451 number reads the `주` in every one of them, so what the
diagonal separates is which notation reaches each row and not which row is
bigger. A holding really is written `2천만주`, so that one is the most
reachable. The
point-in-time silences travel the same way, since this scan does not consult
`_TIME_POINT`:
`２０２１년 착수, 총 3주` asked in years and `１５일 마감, 3주` asked in days are
silent now, where their ASCII spellings were already silent, and
`test_a_point_in_time_silence_travels_into_the_new_notations` holds that half
because it is not this class. Read none of these as a set. The rule is that the
three notations join every reading this scan already makes, and a count here
would be a count of an open class -- which is what the earlier draft of this
paragraph got wrong by saying two, since a rule that omits a notation prices
none of the values only that notation reaches.

One reading in the other direction arrived with them: `３시간30분` asked in
`몇 시간인가?` was told it states minutes and is silent now, which is the defect
of the paragraph above this one, reaching a notation it could not before.
"""


def _value_states_asked_unit(value: str, asked_unit: str) -> bool:
    """Whether the value carries a quantity in the unit the question asked for.

    Read with `_VALUE_MEASUREMENT_RELAXED`, which does not read what
    `_value_measure_units` reads, and the differences run in both directions. No
    count is written here, because a count is what goes short the next time one
    is added -- #453 added one. What the differences are is a diff of the two
    patterns and of the two callers, not a number in this line.

    It reads MORE where the NUMBER is wider -- any Unicode decimal digit rather
    than ASCII, a run of magnitude words rather than one, a wider magnitude class
    -- and where the trailing lookahead is absent. That those widenings read more
    and never less is argued from the character sets rather than sampled; the
    argument is in `_VALUE_MEASUREMENT_RELAXED` and its premise is asserted by
    `test_the_widened_number_can_only_silence`. It is worth knowing which of the
    two the tests are doing, because `finditer` resumes from a match's end and a
    longer match could in principle hide a later one: the premise closes that,
    and the corpus sweep confirms it.

    It reads LESS where `_UNIT_SHADOW_GUARD` refuses a spelling that a word in
    `_UNIT_SHADOW_WORDS` stands complete in place of, which is #453.

    And the comparison is with a FUNCTION and not only with that pattern, so it
    differs where the function does: `_value_measure_units` drops a quantity
    overlapping a `_TIME_POINT` span and this scan does not consult that guard,
    so `_value_states_asked_unit("매월 15일 소요", "DAY")` is True where
    `_value_measure_units("매월 15일 소요")` is empty. `_TIME_POINT` states that
    difference from its own side.

    Every unit `_value_measure_units` reports is still caught here, so this still
    subsumes the plain equality test it replaced -- but the narrowing means that
    no longer follows from the character sets, and it now rests on the criterion
    a member of `_UNIT_SHADOW_WORDS` has to meet: the member's complete form is
    one the REPORTING scan reads as no quantity at all, so where the guard
    refuses there was no reported reading to subsume.
    `test_a_shadow_word_is_one_the_reporting_scan_already_refuses` is where that
    criterion is enforced and
    `test_the_suppression_scan_sees_everything_the_value_scan_sees` is where the
    subsumption itself is swept. A member breaking the criterion reddens both,
    which is measured rather than assumed -- `일`, `주`, `일간` and `분간` each
    do.
    """
    folded = nfc(value).casefold()
    return any(
        _MEASUREMENT_UNIT_SPELLINGS[match.group("unit")] == asked_unit
        for match in _VALUE_MEASUREMENT_RELAXED.finditer(folded)
    )


def _question_measure_unit(question: str) -> tuple[str, str] | None:
    """The unit a Korean measure question asked in -- its last, if it names two.

    Returned as (spelling, unit). The singular in a summary line is worth
    qualifying because a two-counter question falsifies it:
    `기간은 몇 년 몇 개월인가?` yields `개월`, so against a `2년` value this rule
    would name a mismatch against a question that did ask in years. That is
    latent rather than user-visible -- the cleaned label `기간은 몇 년` names no
    relation an ordinary KB holds, so the question is declined to the model and
    never reaches a verified answer to be caveated. Only a KB carrying a
    relation spelled `기간은 몇 년` could surface it.

    None when the question is not the flat attribute shape; when its label has no
    measure tail; when the tail carries a counter that names no unit
    (`몇 개인가?`) or no counter at all (`얼마나 되나요?`); or when what stands in
    front of the tail does not read as a relation.

    That last condition is what keeps a KB holding a relation literally named
    `몇 년` out of this rule. There the tail is the whole label, nothing is left
    in front of it, and `_korean_attribute_label_readings` reads the label whole
    -- the question is asking *for* that relation, not *in* that unit.
    """
    match = _KOREAN_ATTRIBUTE_QUESTION.match(question.strip())
    if match is None:
        return None
    label = " ".join(match.group("label").strip().split())
    tail = _KOREAN_ATTRIBUTE_LABEL_MEASURE_TAIL.search(label)
    if tail is None:
        return None
    spelling = tail.group("counter")
    # `.get`, so the two ways there is no unit to ask about -- a counter outside
    # the table and the `얼마나` branch, which captures no counter at all --
    # arrive at the same answer without a branch apiece.
    unit = _MEASUREMENT_UNIT_SPELLINGS.get(spelling)
    if unit is None:
        return None
    if not _label_readings_after_measure(label[: tail.start()].strip()):
        return None
    return (spelling, unit)


def _value_measure_units(value: str) -> tuple[tuple[str, str], ...]:
    """The units a value states, as this pattern reads them.

    Each is a (unit, spelling) pair, in the order stated.

    "As this pattern reads them" is load-bearing rather than hedging: `3시간30분`
    states hours and this returns only minutes, because the lookahead refuses a
    unit followed by a digit. `korean_measure_unit_mismatch` depends on that
    being the reporting reading and on a second, looser one deciding
    suppression.

    Empty for a value stating no quantity. A value carrying a point in time is
    read, and the quantities that OVERLAP the point's own span are the ones
    dropped -- overlap and not containment, which `_TIME_POINT` argues and
    `2021-03-15일` witnesses, since a quantity can begin inside a span and end
    outside it. `_value_measure_units("매월 15일 소요")` is empty because the
    `15일` is the day of the month, while `_value_measure_units("매월 15일, 3주")`
    reports the three weeks. `_TIME_POINT` states the rule and what it costs.

    NFC because a value written in NFD spells `년` as two code points while the
    alternation is composed, and casefold because the Latin spellings in the
    table are lower-case. One consequence is worth naming: the spelling handed
    back is the folded one, so a value stating `3 Weeks` is reported as stating
    `weeks`.
    """
    folded = nfc(value).casefold()
    points = [point.span() for point in _TIME_POINT.finditer(folded)]
    return tuple(
        (_MEASUREMENT_UNIT_SPELLINGS[match.group("unit")], match.group("unit"))
        for match in _VALUE_MEASUREMENT.finditer(folded)
        if not any(
            match.start() < point_end and point_start < match.end()
            for point_start, point_end in points
        )
    )


def korean_measure_unit_mismatch(question: str, value: str) -> tuple[str, str] | None:
    """The (asked counter, stated unit) a unit caveat should name, or None.

    A mismatch only when the value states a unit in the same family as the one
    asked for and a second scan finds no quantity in the asked unit itself.

    Both halves are what a pattern reads, not what the value contains, and the
    sentence has to be put that way round: `_value_states_asked_unit` re-reads
    the value for the asked unit with a wider quantity shape and no TRAILING
    lookahead, and anything that scan cannot see is not suppressed on. It reads `6개월` in
    `2년 6개월`, so a `몇 개월인가?` is suppressed, and since #451 it reads the
    won in `2천만원 (15,000달러)` and `2백만원 (15,000달러)` and the years in
    `３년 30주` as well.

    What it still cannot see is given as a rule in
    `_VALUE_MEASUREMENT_RELAXED`, and the rule is the thing to read, because
    every attempt to write the residue as a list has left something out. The
    scan reads a unit ONLY where SOME decimal digit stands before that
    unit's spelling with nothing between them but digits, separators, class
    magnitudes and whitespace -- necessary and not sufficient, since the
    alternation taking `달러` before `달` can refuse a unit that satisfies it.
    Some DIGIT and not every digit, or `1경5천조원`
    reads wrong: it qualifies on its `5` and not on its `1`. The noun is worth
    repeating because ranged over occurrences of the SPELLING the same words
    say something false -- `2천만원 지원` holds two `원` and is read on the
    first. So `한 시간 30분` and `반년 3주`
    fail for want of any digit -- as `이천만원` does, and the native-numeral and
    no-numeral forms are the reachable members of that class rather than the
    Sino-Korean one -- while `1경원`, `3천만여원` and `20여년` have digits whose
    gaps are all blocked. The other half of the residue is a spelling
    `_MEASUREMENT_UNIT_SPELLINGS` leaves out on purpose: the `개년` in
    `5개년 계획 3주` and the bare `월` in `6월 및 30주`. #451 widened what may
    stand in a gap, and widened which characters count as digits; the
    requirement that there be a digit at all is older than it and survives it,
    and the bullet below is where the survivors live.

    The two halves are read by different patterns, and that is deliberate rather
    than an oversight. What the value STATES comes from `_value_measure_units`,
    which refuses a unit run into the next character and reads ASCII digits and
    at most one of `[만억천조]`. Whether the value CARRIES the asked unit comes from
    `_value_states_asked_unit`, which differs from it in both directions and
    sets those differences out. Add a part to either pattern and that is the
    paragraph to correct: it reads the pattern and sits beside it, which is why
    the description lives there and not here. With one pattern
    doing both, the same lookahead that correctly declines to read `2년차` as two
    years also hid the asked unit in `3시간30분` and `2년6개월`, and the caveat
    fired on a value whose leading quantity was exactly what the question asked
    for. Spacing decided it, which no reader would predict; #451 was the same
    defect one layer down, where the notation the number was written in decided
    it instead.

    The divergence is permitted in one direction, and the rule is about READING
    MORE rather than about every change to these patterns. Reading more with the
    relaxed pattern ends caveats and cannot start one, since it feeds a single
    early return here; reading more with `_value_measure_units` is what a reader
    is shown, so it adds sentences and is a separate change with a sweep of its
    own.

    Reading LESS with the relaxed pattern is the case an unscoped version of that
    sentence was read as covering, and it runs the other way: it STARTS caveats.
    `3분기 실적, 2시간 소요` asked in minutes is silent without
    `_UNIT_SHADOW_GUARD` and names its `시간` with it, which is the whole of #453.
    So a narrowing here cannot be cleared by a witness -- a witness shows one
    caveat gained and says nothing about the ones gained beside it -- and what
    #453 rests on instead is the `CONTINUED` cell of
    `test_the_shadow_guard_gains_no_caveat_on_a_word_it_only_prefixes` being
    empty, which is a claim about a population rather than about a value.

    The first same-family unit is reported, not the first unit. `30% 완료, 3주`
    asked in months states a ratio first and a duration second, and the duration
    is the part the question was about.

    The main causes of an accepted silence, rather than all of them: a value
    stating no number; a unit run into the next syllable (`2년차`); a quantity
    that overlaps a point in time (`매월 15일`); a spelling outside the
    table; a suffix outside `_UNIT_SUFFIX`;
    a number that stacks magnitude words (`2천만원`) or uses one outside
    `[만억천조]` (`2백원`), since `_VALUE_MEASUREMENT` admits at most one and
    only from those four -- `2백만원` needs both allowances and is silent for
    either reason; and a number written in full-width digits (`３년`),
    which its `[0-9]` does not admit and `nfc` does not fold away. `nfkc` would
    fold it, but `verinote.text.nfc` is the one normalizer the rest of the
    codebase compares through, and folding compatibility forms here alone would
    have this rule read a value differently from every other comparison made on
    it. A non-breaking space between the number and the unit is fine
    (`3<NBSP>년` states years), so
    this silence is specifically the digits. Those last two are silences on the
    REPORTING side only since #451. The suppression scan reads all three
    notations, so where the ASKED unit is the one written that way the whole rule
    now says nothing instead of naming a neighbour: `2천만원 (15,000달러)` asked
    in won, `３년 30주` asked in years. Asked in some other unit of the family the
    quantity is still unreported and a neighbour can still be named --
    `３년 30주` asked in months says `주` -- which is the reporting silence doing
    what it has always done. An earlier cross-family quantity
    does not silence a later same-family one.

    One silence is worth separating from those, because it is the only one where
    the value did earn a caveat and this rule loses it by misreading rather than
    by not reading. `_value_states_asked_unit` refuses a spelling where a word in
    `_UNIT_SHADOW_WORDS` stands complete in its place, so `3분기 실적, 2시간 소요`
    asked in minutes names its `시간` again. Three residues are left, and none of
    them is a remainder of the others. A spelling that means the other thing with
    NOTHING appended has no longer word to list at all -- `3백주 보유, 3개월 준비`
    is three hundred shares and is silent still, and #467 is where that half is
    filed. A longer word the tuple does not hold is reachable and simply absent:
    `3분야 검토, 2시간 소요` and `3 secondaries, 2 minutes`. And a listed word
    continued flush by another letter is refused refusal on purpose --
    `3분기실적, 2시간 소요` stays silent, which is the price of not caveating
    `30분기준 회의, 2시간 소요` against a value that does state thirty minutes.
    `_VALUE_MEASUREMENT_RELAXED` carries the rule and the two defaults it rests
    on.

    Wrong sentences this is known to produce. These are the ones that have been
    found, not a bound on what exists, and each round of review has added to
    them; read the list as open. They also have no single cause -- a two-digit
    year is not a lexical ambiguity and `second` is not Korean -- so do not
    generalise from it to decide whether some new input is safe.

    * A day of the month with no month term in front of it, in the sense
      `_TIME_POINT` defines one. `15일 마감` is the deadline
      on the fifteenth and is told it states `일`, fifteen days. Nothing in the
      value separates it from `15일 소요`, which really is fifteen days -- only
      the trailing noun does, and that noun does not partition, since `마감`
      heads both readings. A 마감일 relation holding a bare `N일` is ordinary
      contract data, which makes this the most reachable one found so far. It
      reaches further since
      #452: a bare day is a point in time no `_TIME_POINT` branch defines, and
      the whole-value guard used to silence it whenever anything else in the
      value happened to be a date, so `2021년 계약, 15일 마감` was silent and now
      states `일`. The misreading is the same one; only its reach changed.
    * A date written with no year. The ISO branch needs two separators, so
      `03/15일` has one and is reported as stating `일`. Reading a slashed month
      was tried for #450 and withdrawn: `[0-9]{1,2}\\s*/` also reads the numerator
      of a small-number rate, so `50/15일` and `10/30일` stopped being read at all
      and `50/15일 3주` lost a genuine caveat. A dotted form is worse, since
      `1.5일` is a decimal duration with the same shape, and a dashed one collides
      with the range `10-15일`.
    * A value whose asked-unit quantity the suppression scan cannot read, beside
      a same-family unit the reporting scan can, so the caveat names the second.
      What makes the two kinds below worth telling apart is that one is a
      pattern's reach and the other is a table's contents; the split is not a
      claim that there are two of anything, and the NUMBER side is itself given
      as a rule rather than a list, for the reason
      `_VALUE_MEASUREMENT_RELAXED` gives.

      The NUMBER, in the two ways `_VALUE_MEASUREMENT_RELAXED`'s rule can
      fail. That rule is existential -- SOME digit before the unit with a clean
      gap -- so both failures below are failures at every digit, not at one.
      These are the values the rule EXCLUDES; the rule holds only in that
      direction, and the two classes it declines while satisfying are recorded
      with it rather than here.

      No DIGIT at all before the asked unit, so the scan has nothing to start
      from. `이천만원 (15,000달러)` asked in won reports `달러`, and so do
      `한 시간 30분` asked in hours, `두 달 3주` and `이틀 3주` asked in their
      own units, and `반년 3주`, `수개월 3주` and `수십억원 (15,000달러)`,
      which carry no numeral to spell at all. `한 시간 30분` is the one to
      weigh: it is the very sentence this scan exists to prevent, on the way an
      hour and a half is ordinarily written, since `일 시간` is not Korean.
      Notation and not quantity -- `1시간 30분` and `1.5시간` say the same thing
      and are both silent -- which is what makes it a spelling defect rather
      than a limit on what can be asked.

      Something in the GAP between that digit and the unit. A magnitude outside
      `_SINO_KOREAN_MAGNITUDES` (`1경원 (15,000달러)`), or an approximator
      (`3천만여원 (15,000달러)`, `20여년 3주`) -- position and not vocabulary,
      since the same `여` after the unit is harmless.

      #451 moved the gap condition and part of the digit one: it widened what
      magnitudes may stand in the gap, and widened the digits themselves from
      `[0-9]` to every Unicode decimal digit. What it did not touch is the
      requirement that the starting character be a DECIMAL DIGIT at all, which
      is why the whole first group above is older than #451 and survives it. It
      retired `2천만원 (15,000달러)` and `３년 30주` from this bullet, which is
      what it listed before; `1억5천만원 및 20,000달러` was in the test table
      rather than here, and `2백만원 (15,000달러)` was in neither, since this
      change is what found it.

      The SPELLING: a row `_MEASUREMENT_UNIT_SPELLINGS` excludes on purpose.
      `5개년 계획 3주` asked in years reports `주`, and `6월 및 30주` asked in
      months reports `주`, for the reasons given beside those exclusions.
      Reading either on the suppression side was measured against #451 and
      declined. Bare `월` silences `3월 15일간`, `3월 내 15일 소요` and
      `3월 계약 15일 소요` among others, each of which states a duration some
      test asserts is caveated -- the word-month forms beside them,
      `전월 대비 3일 단축` and `매월 15일간`, are untouched, since no digit
      stands before their `월` -- and declining a `월` inside a `_TIME_POINT`
      span does not even reach the two
      the year+month branch's bounds are placed for, since neither `2년 3월` nor
      `10000년 3월` matches that pattern at all. `개년` silences a `5개년` the
      table calls the name of a plan rather than a duration, which is the
      judgement the row was excluded for.

      #451 states the general form these are instances of: an accepted silence
      here is a latent wrong sentence, and a readable same-family unit standing
      beside it is what turns it into one. The form reaches past this bullet --
      `₩20,000,000 (15,000달러)` asked in won reports `달러` for a third reason
      again, the symbol standing where no row in `_MEASUREMENT_UNIT_SPELLINGS`
      can reach it.
    * A bare two-digit year. `21년 3월` is read as a date and so is `'21년`, but
      `21년` alone is left reading YEAR, so `몇 개월인가?` answered `21년` is told
      it states years. Twenty-one years is a real duration, and without the
      apostrophe or a month beside it nothing here separates the two readings.
      As with the bare day above, #452 widened the reach rather than the
      misreading: `2021년 착수, 21년` was silenced by the four-digit year
      standing next to it and now states `년`.
    * The second member of a date or clock list whose head is elided. Korean
      writes two days of one month as `3월 15일~20일` or `매월 15일, 30일`, and
      the branch takes the first member only, so the second stands outside every
      span and is read as a quantity: both of those are told they state `일`, and
      so are `3월 15일-20일`, `3월 15일·20일`, `3월 15일 및 20일`, `3월 15일과 20일`,
      `3월 15일 또는 20일`, `3월 15일(월), 20일(토)`, `매월 10일 / 25일`,
      `다음 달 1일, 15일`, `3월 초 5일, 15일` and `매월 5일, 15일, 25일 지급`.
      `2021년, 22년` is the same shape on the year and `3시 30분 ~ 45분` on the
      clock. Each was silent before #452, because the whole-value guard spent
      its match on the second member as well.
      `3월 15일부터 20일까지` is silent, and that is a spacing accident rather
      than a rule -- the trailing lookahead refuses `20일까지`, and the spaced
      `3월 15일 부터 20일 까지` is caveated.
      Unlike the bare day above there IS a month term left to read, so this one
      is reachable in principle and is filed rather than accepted. Two
      candidates were measured. A bounded tilde continuation on the day reaches
      `~` and none of the other separators, and that set is an open lexical
      class. Suppressing a leaked quantity whose unit a span already states
      reaches every separator, but only where the head's own component survives
      the trailing lookahead -- `3월 15일, 20일` yes, `3월 15일과 20일` no -- and
      reaches no ISO or slashed head, whose spans state no unit to repeat. It
      would also silence `매월 15일 마감, 3일 이내 지급` and `3시 30분 회의, 30분 연장`,
      which state a genuine duration that happens to share a unit with the date
      beside it.
    * An English ordinal. `second` is in the spellings table as a unit, so
      `2 second review` is reported as stating `second`. The neighbouring
      `3 secondary reviews` is silent for an unrelated reason -- Latin continues
      past the spelling and the lookahead refuses it -- which does not reach the
      spaced form. The row stays, because `30 seconds` answering `몇 분인가?` is
      a real result and the question side can only ask SECOND through `몇 초`.
      Since #453 the suppression scan refuses `secondary` too, so
      `3 secondary reviews, 2 minutes` asked in `몇 초` names the minutes.
      `3 secondaries, 2 minutes` does not: `secondary` is not a prefix of
      `secondaries`, so the word is simply absent from the tuple. The reporting
      reading of the spaced `2 second review` is untouched, which is what keeps
      this bullet here.
    * `몇 년인가?` answered `100주` (one hundred shares), and `몇 시간인가?`
      answered `5분` (five people, honorific): Korean spellings that mean two
      things, read here as the unit. These two also need a question asked in a
      unit the relation does not really measure.
      This is also the residue of the syllable class
      `_VALUE_MEASUREMENT_RELAXED` records, in its silent form:
      `3백주 보유, 3개월 준비` and `2천만주 보유, 3개월 준비` asked in weeks are
      the same shares reading producing a lost caveat instead of a sentence.
      #467 carries both forms.
    * The day itself, in a value where a free word rather than a bound suffix
      marks the duration. The day branch consults only what is written flush
      against `일`, so `매월 15일간` is read as a duration while `매월 15일 동안`
      is a day of the month with a word after it, and the fifteen days it really
      states are never reported. `남짓`, `내내`, `이상` and any other free word
      implying a span behave alike, and the class is not bounded by that list --
      "means a duration" is open, which is why the branch keys on being bound
      instead. #458 tracks moving the lookahead's position, which is what would
      reach these; listing the words would not, because standard orthography
      spaces them.
      Two mechanisms produce that one silence and they are worth telling apart,
      since conflating them is what this file keeps having to correct. In
      `매월 15일 동안` the `15일` IS read by `_VALUE_MEASUREMENT` and is then
      dropped for overlapping the day's span; in `매월 15일동안` no quantity is
      read at all, because the trailing lookahead refuses a unit run into
      Hangul. Same outcome, different cause, and only the first would move if
      the span moved.
      What #452 did change is the rest of such a value. The guard used to be
      whole-value, so a day read as a date silenced every other quantity beside
      it and `매월 15일동안 3주` said nothing about its three weeks; span-local
      reports them. The day is still lost either way, so #458 is narrowed rather
      than closed.
    * A quantity overlapping a point-in-time expression. `매월 3일 소요` is the
      third of each month and states no duration this rule can see, so a
      `몇 주인가?` against it is silent; the same goes for the `15일` in
      `3월 중 15일` and the `30분` in `3시 30분`. This is the guard working rather
      than failing, and it is listed here because the reading is a judgement:
      `매월 3일 소요` can be read as three days a month, and `_TIME_POINT` argues
      for the other reading. What is no longer true is that the loss spreads --
      #445 and #450 made it whole-value, so any duration anywhere else in the
      value went with it, and #452 confined it to the span.

    None of these is fixed here. #445 asks that a verified answer in another unit
    be caveated, not that every ambiguous spelling be resolved; #450 added the
    point-in-time guard an earlier version of this paragraph asked for, and #452
    made it span-local, and neither changed that. Two of the entries above are
    beyond any widening of `_TIME_POINT` for the same reason: in `15일 마감` and
    in `21년` the notation for a point in time is identical to the notation for a
    quantity, so there is nothing left in the value to read. Do not generalise
    that to the rest of the list -- the elided second member has a month term
    standing right in front of it, and what has stopped it being fixed is that
    every rule tried so far reaches some spellings and not others.
    """
    asked = _question_measure_unit(question)
    if asked is None:
        return None
    asked_spelling, asked_unit = asked
    found = _value_measure_units(value)
    if _value_states_asked_unit(value, asked_unit):
        return None
    asked_family = _MEASUREMENT_FAMILY[asked_unit]
    for unit, spelling in found:
        if _MEASUREMENT_FAMILY[unit] == asked_family:
            return (asked_spelling, spelling)
    return None
