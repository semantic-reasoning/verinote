# SPDX-License-Identifier: MPL-2.0
"""The wirelog engine must produce findings the source-label annotator can use.

`annotate_source_labels` names the facts behind a finding by reading the
finding's row positionally, and it is only allowed to do that when the row says
which rule it came from and how that rule declared its columns. Both halves are
filled in by the engine backend, so a backend that carries values but forgets
the shape produces a report that still reads fine and silently names no facts.

The DuckDB half of that contract is pinned in `test_engine_input.py`; this is
its wirelog twin. It goes end to end on purpose: not "is the field set" (that is
`test_engine.py`) but "does the line the user reads actually get its note".
"""

from pathlib import Path

import pytest

from verinote.engine import DEFAULT_POLICY, compile_dl, run_check
from verinote.engine.terms import StringLit
from verinote.pipeline.engine_input import annotate_source_labels, engine_relation_rows
from verinote.store import Store


def _store(tmp_path) -> Store:
    s = Store(Path(tmp_path) / "kb.sqlite")
    s.init_schema()
    return s


def _text(term: object) -> str:
    assert isinstance(term, StringLit)
    return term.value


def test_wirelog_findings_name_the_conflicting_facts_in_the_source_s_own_words(tmp_path):
    """A report about `established_on` is unreadable to a KB that only said `설립`.

    The default alias table already maps `설립 -> established_on`, so the engine
    gates on a relation label this KB never used. The note is the only thing
    that ties the finding back to the facts the user wrote.

    The wirelog engine only runs when pyrewire is installed; without it
    `run_check` returns an `engine_available=False` compatibility report and the
    assertion would be vacuous, so this skips rather than passes (see #234).
    """
    pytest.importorskip("pyrewire")
    s = _store(tmp_path)
    first = s.add_fact("회사", "설립", "2020", status="accepted")
    second = s.add_fact("회사", "설립", "2021", status="accepted")

    rows = engine_relation_rows(s)
    rep = run_check(
        compile_dl(
            [
                {key: _text(row[key]) for key in ("subject", "relation", "object")}
                for row in rows
            ]
        ),
        policy_dl=DEFAULT_POLICY,
    )
    annotated = annotate_source_labels(rep, rows)

    errors = [f for f in annotated.findings if f.startswith("ERROR ")]
    assert errors == [
        f"ERROR functional_conflict: 회사 established_on "
        f"(설립 #{first}=2020, 설립 #{second}=2021)"
    ]
    assert errors[0] in annotated.text
