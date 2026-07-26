# SPDX-License-Identifier: MPL-2.0
"""Small CSS declaration guard for state signals that must survive without colour."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
import unicodedata

RULE = re.compile(r"(?P<selector>[^{}]+)\{(?P<body>[^{}]*)\}")
COLOUR_PROPERTY = re.compile(r"^(color|background|border(-[a-z]+)*-color|outline-color)$")
COLOUR_VAR = re.compile(
    r"var\(\s*--(?:ok|warn|danger|accent|line|muted|panel|fg|bg|term)[a-z-]*\s*\)"
)
BORDER_STYLE = re.compile(r"^(border|outline)(-[a-z]+)*-style$")
BORDER_WIDTH = re.compile(r"^(border|outline)(-[a-z]+)*-width$")
DRAWN_BORDER_STYLES = frozenset(
    {"solid", "dashed", "dotted", "double", "groove", "ridge", "inset", "outset"}
)
INERT_VALUES = frozenset({"none", "normal", "hidden", "auto", "initial", "unset", "revert"})
ZERO_LENGTH = re.compile(r"^0[a-z%]*$")
DRAWN_CONTENT = re.compile(r'^"([^"]*)"')
CSS_ESCAPE = re.compile(r"\\([0-9a-fA-F]{1,6})\s?")
INVISIBLE_CATEGORIES = frozenset({"Zs", "Zl", "Zp", "Cc", "Cf"})
SIGNAL_PROPERTY = re.compile(
    r"^((border|outline)(-[a-z]+)*|box-shadow|content|background-image"
    r"|text-decoration(-[a-z]+)?|font-weight|font-style|text-transform)$"
)
FUNCTION_EDGE_WHITESPACE = re.compile(r"(?<=[(,])\s+|\s+(?=[,)])")


def parse_rules(css: str) -> list[tuple[str, str]]:
    """Return flat CSS rules after removing comments.

    This is deliberately shallow static analysis: it does not model nesting,
    selector specificity, or inheritance.  For repeated exact selectors it does
    apply declaration order (and ``!important``) within that selector scope.
    """
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [(match.group("selector").strip(), match.group("body")) for match in RULE.finditer(css)]


def _declarations(body: str) -> list[tuple[str, str]]:
    declarations = []
    for chunk in body.split(";"):
        prop, sep, value = chunk.partition(":")
        if sep:
            normalized = " ".join(value.split())
            declarations.append(
                (prop.strip(), FUNCTION_EDGE_WHITESPACE.sub("", normalized))
            )
    return declarations


def selector_declarations(css: str, selectors: Sequence[str]) -> list[tuple[str, str]]:
    """Collect exact selectors' declarations, tagging element or ``::before`` scope."""
    wanted = set(selectors)
    found = []
    for selector, body in parse_rules(css):
        for part in (part.strip() for part in selector.split(",")):
            if part not in wanted:
                continue
            scope = "::before" if part.endswith("::before") else ""
            found.extend((f"{scope}|{prop}", value) for prop, value in _declarations(body))
    return found


def effective_selector_declarations(
    css: str, selectors: Sequence[str]
) -> list[tuple[str, str]]:
    """Return the final applicable declaration per exact selector scope and property.

    The parser intentionally only knows exact selectors, so this is not a general
    cascade implementation.  Within one exact selector, though, later declarations
    win unless an earlier declaration is ``!important``.
    """
    effective: dict[str, tuple[str, bool]] = {}
    for prop, value in selector_declarations(css, selectors):
        clean_value = value.removesuffix("!important").rstrip()
        important = clean_value != value
        previous = effective.get(prop)
        if previous is None or important or not previous[1]:
            effective[prop] = (clean_value, important)
    return [(prop, value) for prop, (value, _) in effective.items()]


def _paints(text: str) -> bool:
    return any(unicodedata.category(char) not in INVISIBLE_CATEGORIES for char in text)


def _drawn(prop: str, value: str) -> str | None:
    base = prop.split("|")[-1]
    if value in INERT_VALUES:
        return None
    if BORDER_STYLE.match(base):
        return value if value in DRAWN_BORDER_STYLES else None
    if BORDER_WIDTH.match(base):
        return None if ZERO_LENGTH.match(value) else value
    if base == "content":
        match = DRAWN_CONTENT.match(value)
        if not match:
            return None
        glyph = CSS_ESCAPE.sub(lambda match: chr(int(match.group(1), 16)), match.group(1))
        return glyph if _paints(glyph) else None
    return value


def non_colour_drawn_signals(css: str, selectors: Sequence[str]) -> frozenset[tuple[str, str]]:
    """Return visible, non-colour signals for an exact selector group.

    A ``::before`` rule without non-empty drawn ``content`` is ignored entirely: without
    a glyph it is not a usable drawn signal for this guard.
    """
    signals = set()
    for selector in selectors:
        declarations = effective_selector_declarations(css, (selector,))
        values = dict(declarations)
        pseudo_has_content = _drawn(
            "::before|content", values.get("::before|content", "none")
        ) is not None
        pseudo_is_displayed = values.get("::before|display") != "none"
        for prop, value in declarations:
            scope, base = prop.split("|", 1)
            if scope == "::before" and (not pseudo_has_content or not pseudo_is_displayed):
                continue
            if COLOUR_PROPERTY.match(base) or not SIGNAL_PROPERTY.match(base):
                continue
            stripped = " ".join(COLOUR_VAR.sub(" ", value).split())
            if not stripped:
                continue
            drawn = _drawn(prop, stripped)
            if drawn is not None:
                signals.add((prop, drawn))
    return frozenset(signals)


def assert_pairwise_distinct_signals(
    css: str, selector_groups: Mapping[str, Sequence[str]]
) -> dict[str, frozenset[tuple[str, str]]]:
    """Assert every selector group has a unique non-colour drawn signal set.

    A baseline state may intentionally have no extra signal.  It is sufficient that
    its actual set differs from every other state, rather than requiring every state
    to contribute a signal beyond a common intersection.
    """
    signals = {
        name: non_colour_drawn_signals(css, selectors)
        for name, selectors in selector_groups.items()
    }
    assert len(set(signals.values())) == len(signals), (
        "selector groups do not have pairwise distinct non-colour signals: "
        f"{ {name: sorted(found) for name, found in signals.items()} }"
    )
    return signals
