# SPDX-License-Identifier: MPL-2.0
"""Cross-component regression guard for non-colour CSS state signals."""

from __future__ import annotations

from pathlib import Path
import re

import pytest

from css_signal_guard import assert_pairwise_distinct_signals, non_colour_drawn_signals

CSS_PATH = Path(__file__).resolve().parents[1] / "verinote" / "web" / "static" / "app.css"

ASK_VERDICTS = {
    "verified": (
        ".ask-verdict-verified .ask-verdict",
        ".ask-verdict-verified .ask-verdict::before",
    ),
    "verified-negative": (
        ".ask-verdict-verified-negative .ask-verdict",
        ".ask-verdict-verified-negative .ask-verdict::before",
    ),
    "unverified": (
        ".ask-verdict-unverified .ask-verdict",
        ".ask-verdict-unverified .ask-verdict::before",
    ),
}
BANNERS = {
    "ok": (".ok-note", ".ok-note::before"),
    "warn": (".warn", ".warn::before"),
    "error": (".error", ".error::before"),
}
BADGES = {
    "confirmed": (".badge-confirmed", ".badge-confirmed::before"),
    "accepted": (".badge-accepted", ".badge-accepted::before"),
}
PROGRESS = {
    "running": (".progress-complete",),
    "done": (".progress.is-done .progress-complete",),
}
SIGNAL_REGISTRY = (ASK_VERDICTS, BANNERS, BADGES, PROGRESS)


def _css() -> str:
    return CSS_PATH.read_text(encoding="utf-8")


def _mutate_declaration(
    css: str, selector: str, property_name: str, replacement: str | None
) -> str:
    """Replace or remove a declaration without making rule-formatting assumptions."""
    rule = re.compile(
        rf"(?P<start>{re.escape(selector)}\s*\{{)(?P<body>[^}}]*)(?P<end>\}})"
    )
    match = rule.search(css)
    assert match, f"missing CSS rule for {selector}"
    declaration = rf"\s*{re.escape(property_name)}\s*:\s*[^;]*;"
    if replacement is None:
        body, count = re.subn(declaration, "", match["body"], count=1)
    else:
        body, count = re.subn(
            rf"({re.escape(property_name)}\s*:\s*)[^;]*(;)",
            lambda declaration: f"{declaration[1]}{replacement}{declaration[2]}",
            match["body"],
            count=1,
        )
    assert count == 1, f"{selector} has no {property_name} declaration"
    return css[: match.start()] + match["start"] + body + match["end"] + css[match.end() :]


@pytest.mark.parametrize("selector_groups", SIGNAL_REGISTRY)
def test_state_selector_groups_have_pairwise_distinct_non_colour_signals(
    selector_groups: dict[str, tuple[str, ...]],
) -> None:
    assert_pairwise_distinct_signals(_css(), selector_groups)


@pytest.mark.parametrize("replacement", ['"" / ""', None])
def test_pseudo_without_non_empty_content_does_not_count_as_a_signal(
    replacement: str | None,
) -> None:
    selector = ".ask-verdict-verified .ask-verdict::before"
    mutated = _mutate_declaration(_css(), selector, "content", replacement)

    signals = non_colour_drawn_signals(
        mutated,
        (".ask-verdict-verified .ask-verdict", selector),
    )
    assert not any(prop == "::before|content" for prop, _ in signals)


@pytest.mark.parametrize("declaration", ('content: none;', 'content: "";', 'display: none;'))
def test_later_pseudo_declaration_overrides_an_earlier_glyph(declaration: str) -> None:
    """An exact selector's later declaration controls whether its pseudo can signal."""
    selector = ".ask-verdict-verified .ask-verdict::before"
    baseline = non_colour_drawn_signals(
        _css(),
        (".ask-verdict-verified .ask-verdict", selector),
    )
    assert ("::before|content", "✓") in baseline

    mutated = f"{_css()}\n{selector} {{ {declaration} }}\n"
    signals = non_colour_drawn_signals(
        mutated,
        (".ask-verdict-verified .ask-verdict", selector),
    )

    assert not any(prop.startswith("::before|") for prop, _ in signals)


def test_colour_only_mutation_fails_the_progress_guard() -> None:
    mutated = _mutate_declaration(
        _css(),
        ".progress.is-done .progress-complete",
        "background-image",
        None,
    )

    with pytest.raises(AssertionError, match="colour alone|pairwise distinct"):
        assert_pairwise_distinct_signals(mutated, PROGRESS)


def test_done_progress_uses_a_repeating_terminal_pattern() -> None:
    signals = non_colour_drawn_signals(_css(), PROGRESS["done"])
    assert any(
        prop == "|background-image" and value.startswith("repeating-linear-gradient(")
        for prop, value in signals
    )
