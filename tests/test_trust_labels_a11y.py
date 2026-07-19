# SPDX-License-Identifier: MPL-2.0
"""The trust-signal badges need a *group* label that assistive tech can reach (#294).

The bunch used to sit in `<div class="trust-labels" aria-label="trust signals">`. A
plain `<div>` has the implicit ARIA role ``generic``, and ``generic`` is
*name-prohibited*: a conforming AT may drop an author-provided ``aria-label`` on it
entirely, so the only thing announced was a run of loose badge words with nothing
saying what they were signals *of*.

This is a different contract from the triple slots in ``test_term_kind_a11y.py``,
which is why it lives in its own file. There the label belongs to a *value* and the
fix is to put the kind into a real text node; here the label names a *group*, and
moving it into the visible text would just be noise on the page. The fix instead
gives the label a host that is allowed to carry one -- ``role="list"`` -- and, because
``list`` has required owned elements, marks each badge ``role="listitem"``.

Parsing is stdlib ``html.parser`` on purpose; see the docstring of
``test_term_kind_a11y.py`` for why an undeclared transitive ``lxml`` is not used.
The assertions below deliberately go through the parsed DOM rather than substring
checks on the markup: ``'aria-label="trust signals"' in html`` would still pass with
the role stripped back off, i.e. it would pass on the bug this file exists to pin.
"""

from __future__ import annotations

from html.parser import HTMLParser

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from verinote.config import Config  # noqa: E402
from verinote.store.fact_input import structural_term  # noqa: E402
from verinote.web import create_app  # noqa: E402

GROUP_CLASS = "trust-labels"

# Roles that permit an author-provided accessible name and suit a bunch of labels.
# ``generic`` (the implicit role of a bare div/span) is pointedly not among them.
NAMING_ROLES = {"list", "group", "region"}

# Tags whose implicit role is ``generic``. Naming them requires an explicit role.
GENERIC_TAGS = {"div", "span"}

# Roles that prohibit an author-provided name. Declaring one of these *explicitly* is
# no better than leaving the element generic -- the label is still free to be dropped
# -- so "has a role attribute" is not on its own enough to clear the pin below.
NAME_PROHIBITED_ROLES = {"generic", "presentation", "none", "paragraph"}

NAME_ATTRS = ("aria-label", "aria-labelledby")

# HTML void elements never get an end tag, so they must not enter the open-tag stack.
VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}  # fmt: skip


class _Markup(HTMLParser):
    """Pull out the trust-label group, its direct children, and every named generic.

    ``group`` is the attributes of the ``.trust-labels`` element, ``children`` the
    attributes of each *direct* child element of it (nested descendants do not count
    -- required owned elements are about the immediate children). ``named_generics``
    collects every ``div``/``span`` that carries an accessible name while its role
    prohibits one -- whether it left the role implicit or spelled out a
    name-prohibited role such as ``presentation``.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.group: dict[str, str] | None = None
        self.children: list[dict[str, str]] = []
        self.named_generics: list[tuple[str, dict[str, str]]] = []
        self._stack: list[str] = []
        self._group_depth: int | None = None

    def handle_startendtag(self, tag: str, attrs) -> None:
        self._record(tag, attrs)

    def handle_starttag(self, tag: str, attrs) -> None:
        self._record(tag, attrs)
        if tag not in VOID:
            self._stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID or not self._stack:
            return
        self._stack.pop()
        if self._group_depth is not None and len(self._stack) < self._group_depth:
            self._group_depth = None

    def _record(self, tag: str, attrs) -> None:
        attributes = {k: (v or "") for k, v in attrs}
        role = attributes.get("role", "").strip().lower()
        if tag in GENERIC_TAGS and (not role or role in NAME_PROHIBITED_ROLES):
            if any(a in attributes for a in NAME_ATTRS):
                self.named_generics.append((tag, attributes))
        if self._group_depth is not None and len(self._stack) == self._group_depth:
            self.children.append(attributes)
        if GROUP_CLASS in (attributes.get("class") or "").split():
            self.group = attributes
            self._group_depth = len(self._stack) + 1


def _parse(markup: str) -> _Markup:
    parser = _Markup()
    parser.feed(markup)
    return parser


def _client(tmp_path) -> TestClient:
    cfg = Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider="anthropic",
        model="m",
        api_key=None,
        base_url=None,
    )
    app = create_app(cfg)
    return TestClient(app)


def _fact_with_signals(store) -> int:
    """A fact with a source, so the page renders at least one trust label."""
    sid = store.add_source("sources/x.txt", kind="text")
    return store.add_fact(
        structural_term('person("Ada")'),
        structural_term("has_role"),
        structural_term('role(person("Ada"), "PI")'),
        status="needs_review",
        source_id=sid,
    )


def _provenance(tmp_path) -> str:
    c = _client(tmp_path)
    fid = _fact_with_signals(c.app.state.store)
    return c.get(f"/facts/{fid}/provenance").text


def test_trust_label_group_carries_a_name_on_a_role_that_permits_one(tmp_path) -> None:
    """The group label must sit on a host that is allowed to have a name.

    Both halves matter and neither is enough alone: an ``aria-label`` on a bare div
    may be discarded, and a ``role="list"`` with no name is an unlabelled list.
    """
    group = _parse(_provenance(tmp_path)).group
    assert group is not None, f"provenance renders no .{GROUP_CLASS} element"

    name = next((group[a] for a in NAME_ATTRS if group.get(a)), None)
    assert name, (
        f".{GROUP_CLASS} has no accessible name ({', '.join(NAME_ATTRS)}); the badges "
        "then read as loose words with nothing saying what they are signals of"
    )
    assert group.get("role") in NAMING_ROLES, (
        f".{GROUP_CLASS} names itself {name!r} but its role is "
        f"{group.get('role') or 'generic (no role attribute)'}, which prohibits an "
        f"author-provided name -- an AT may drop the label. Use one of {NAMING_ROLES}."
    )


def test_trust_label_group_and_its_children_agree_on_being_a_list(tmp_path) -> None:
    """Container and children must be a list together, or neither.

    Asserted in both directions, with no skip. Skipping when the container is not a
    ``list`` would leave a real hole: switch the role to ``region`` and keep the
    ``role="listitem"`` badges and you get **orphan listitems** -- items owned by no
    list, which is malformed ARIA and makes an AT misreport their count and position.
    That mutant passes guard 1 (``region`` permits a name), so a skip here means
    nothing catches it. A skip also reads as a green tick in the summary line, which
    is the property PR #273 set out to keep.
    """
    parsed = _parse(_provenance(tmp_path))
    assert parsed.group is not None, f"provenance renders no .{GROUP_CLASS} element"
    assert parsed.children, (
        f".{GROUP_CLASS} rendered no child elements; this fixture adds a source "
        "precisely so at least one trust label exists"
    )

    listitems = [c for c in parsed.children if c.get("role") == "listitem"]
    if parsed.group.get("role") == "list":
        others = [c for c in parsed.children if c.get("role") != "listitem"]
        assert not others, (
            f".{GROUP_CLASS} is role=list but owns non-listitem children: {others}. "
            "role=list requires its direct children to be role=listitem."
        )
    else:
        assert not listitems, (
            f".{GROUP_CLASS} is role={parsed.group.get('role') or 'generic'} but owns "
            f"role=listitem children: {listitems}. A listitem outside a list is "
            "orphaned -- an AT reports its position and count against nothing."
        )


@pytest.mark.parametrize("page", ["review", "provenance"])
def test_no_generic_element_carries_an_author_name(tmp_path, page) -> None:
    """Generalises the #284 span pin: no name on a role that prohibits one.

    Stated as "a div/span whose role prohibits a name" rather than "any
    ``div[aria-label]``" on purpose. The fix above is a div with an aria-label -- a
    legitimate one, because it declares ``role="list"``. Banning the attribute
    wholesale would fail on the fix itself; banning it *where the role forbids it* is
    the actual rule, and it makes this class of defect structurally unable to return.

    Note it is not enough to ask whether a ``role`` attribute is merely *present*:
    ``<div role="presentation" aria-label="...">`` has one and is still name-prohibited.
    The role's value has to be checked against ``NAME_PROHIBITED_ROLES``.

    ``<nav>``, ``<input>`` and ``<select>`` elsewhere in these templates are untouched:
    their implicit roles permit a name, and they are not generic tags to begin with.
    """
    c = _client(tmp_path)
    fid = _fact_with_signals(c.app.state.store)
    markup = (
        c.get("/review").text
        if page == "review"
        else c.get(f"/facts/{fid}/provenance").text
    )

    named = _parse(markup).named_generics
    assert named == [], (
        f"{page} names an element whose role prohibits a name: {named}. A div/span "
        f"with no role is generic, and {sorted(NAME_PROHIBITED_ROLES)} are "
        "name-prohibited too -- either give it a role that permits a name, or put the "
        "text in a visually-hidden text node."
    )
