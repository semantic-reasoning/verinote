# SPDX-License-Identifier: MPL-2.0
from importlib import resources

import pytest

from verinote.prompts import (
    PromptError,
    default_prompt_text,
    delete_prompt_override,
    get_prompt,
    list_prompts,
    prompt_override_path,
    render_prompt,
    save_prompt_override,
)
# From the SUBMODULE on purpose, the way `verinote/web/app.py` does: it is not in
# `verinote/prompts/__init__.py`'s `__all__` (it joined `library.py` in #546).
from verinote.prompts.library import readable_override_text
from verinote.config import Config


def test_packaged_prompt_defaults_are_available():
    names = {prompt.id for prompt in list_prompts()}

    assert {
        "extraction",
        "ollama-extraction",
        "query-translation",
        "query-intent",
        "focused-role-extraction",
        "extraction-limit-hint",
        "claude-json-wrapper",
    } <= names
    assert resources.files("verinote.prompts").joinpath(
        "defaults", "extraction.md"
    ).is_file()
    assert "semantic subject-predicate-object statement" in default_prompt_text(
        "extraction"
    )


def test_query_intent_prompt_states_the_reason_contract():
    """The prompt is the only place the field contracts can be stated up front.

    The schema must keep every property required (OpenAI strict mode) and the
    parser now tolerates advisory fields on any kind, so if the prompt stops
    saying when to fill `reason` and the comparison fields, nothing else pins
    those contracts down (issue #237).
    """
    text = default_prompt_text("query-intent")

    # Assert the halves of each contract, not merely that the words appear: a
    # prompt inverted to "always fill reason" would still contain "reason" and
    # "unknown_or_unsupported", and this pin has to catch that.
    assert "fill it only when kind is unknown_or_unsupported" in text
    assert "For every other kind, leave reason null" in text
    assert "Leave operator, value_type, and value null on every kind other than compare_typed_value" in text
    assert "put the answer's shape in subject, relation, and object instead" in text


def test_query_intent_prompt_supports_anchored_typed_thresholds():
    text = default_prompt_text("query-intent")

    assert "Use compare_typed_value only for an anchored subject and a typed threshold comparison" in text
    assert '"operator":">="' in text
    assert '"value_type":"amount"' in text
    assert "use only a unit listed for that relation in the schema hint" in text
    assert "Synthetic Company revenue at least one credit" in text
    assert "USD" not in text


def test_query_intent_prompt_does_not_steer_aggregate_questions_to_count():
    """The prompt must not ask for another intent the planner cannot plan."""
    text = default_prompt_text("query-intent")

    assert (
        "Do not classify a question as count: aggregate counts cannot be planned yet"
        in text
    )
    assert "reason saying the question needs an aggregate count" in text


def test_kb_prompt_override_wins(tmp_path):
    save_prompt_override(tmp_path, "extraction", "Use only supplied synthetic text.")

    prompt = get_prompt(tmp_path, "extraction")

    assert prompt.source == "override"
    assert prompt.text == "Use only supplied synthetic text."
    assert prompt.default_text != prompt.text
    assert prompt.override_path == tmp_path / "policy" / "prompts" / "extraction.md"


def test_prompt_reset_falls_back_to_default(tmp_path):
    save_prompt_override(tmp_path, "extraction", "Custom extraction prompt.")

    delete_prompt_override(tmp_path, "extraction")

    assert not prompt_override_path(tmp_path, "extraction").exists()
    assert get_prompt(tmp_path, "extraction").source == "default"


def test_prompt_save_rejects_empty_text(tmp_path):
    with pytest.raises(PromptError):
        save_prompt_override(tmp_path, "extraction", "   ")

    assert not prompt_override_path(tmp_path, "extraction").exists()


def test_prompt_save_rejects_missing_required_placeholder(tmp_path):
    with pytest.raises(PromptError, match="\\{qid\\}"):
        save_prompt_override(tmp_path, "query-translation", "Return answer_q1.")


def test_prompt_render_replaces_only_declared_placeholders(tmp_path):
    save_prompt_override(
        tmp_path,
        "query-translation",
        "Return answer_q{qid}(V). Literal JSON braces stay visible: {\"facts\": []}.",
    )

    assert render_prompt(tmp_path, "query-translation", qid=7) == (
        "Return answer_q7(V). Literal JSON braces stay visible: {\"facts\": []}."
    )


def test_prompt_render_requires_values_for_placeholders(tmp_path):
    with pytest.raises(PromptError, match="missing prompt value"):
        render_prompt(tmp_path, "query-translation")


def test_config_extraction_schema_hint_uses_prompt_default(tmp_path):
    cfg = Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider="anthropic",
        model="m",
        api_key=None,
        base_url=None,
        extraction_max_facts_per_chunk=13,
    )

    assert cfg.extraction_schema_hint() == (
        "Extract at most 13 facts from this chunk. Prefer the most explicit "
        "source-backed facts when more facts are available."
    )


def test_config_extraction_schema_hint_uses_kb_override(tmp_path):
    save_prompt_override(
        tmp_path,
        "extraction-limit-hint",
        "Keep at most {max_facts} synthetic facts.",
    )
    cfg = Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider="anthropic",
        model="m",
        api_key=None,
        base_url=None,
        extraction_max_facts_per_chunk=4,
    )

    assert cfg.extraction_schema_hint() == "Keep at most 4 synthetic facts."


def test_unknown_prompt_id_is_rejected(tmp_path):
    with pytest.raises(PromptError):
        get_prompt(tmp_path, "../secret")


def test_readable_override_text_is_total_and_mirrors_get_prompt(tmp_path):
    """`readable_override_text` answers the B/C question `get_prompt` already decided.

    Its one caller (`verinote/web/app.py::_prompts_page`) uses the answer to choose
    whether to hand the user a Reset that deletes this file, so it is a TOTAL
    function: no state of the filesystem below makes it RAISE, because a raise
    there would take the page down instead of letting it choose (#546). Every
    unreadable-or-irrelevant state answers `None`; the one readable state answers
    the NORMALIZED text — the same string `get_prompt` would adopt for the same
    file — so the editor seeded from it cannot diverge from the load.

    The `None` for empty-after-normalization is the discriminator, not a detail:
    `get_prompt` SKIPS such an override and then validates the packaged default,
    so the page must classify a broken DEFAULT (no controls) rather than a stored
    override (an editor + reset that would delete a file the loader never used).
    """
    override = prompt_override_path(tmp_path, "extraction")
    override.parent.mkdir(parents=True, exist_ok=True)

    # A missing path answers None and does not raise.
    assert readable_override_text(override) is None

    # A DIRECTORY at the path answers None and does not raise.
    subdir = override.parent / "a-directory"
    subdir.mkdir()
    try:
        assert readable_override_text(subdir) is None
    finally:
        subdir.rmdir()

    # An EMPTY file answers None — the discriminator, not a detail.
    override.write_text("", encoding="utf-8")
    assert readable_override_text(override) is None

    # A whitespace-only file normalizes to empty and answers None too.
    override.write_text("   \n\t  ", encoding="utf-8")
    assert readable_override_text(override) is None

    # A readable file answers the NORMALIZED text: CRLF -> LF, then stripped —
    # exactly `get_prompt`'s read clause (`_normalize_prompt_text`).
    override.write_text("first line\r\nsecond line\r\n", encoding="utf-8")
    assert readable_override_text(override) == "first line\nsecond line"

    # A file this process cannot decode answers None and does not raise.
    override.write_bytes(b"at most {max_facts} facts \xff\xfe and a bad byte")
    assert readable_override_text(override) is None

    # A mode-0o000 FILE answers None. The probe is the same runtime one the
    # sibling file's local `_broken_override` uses: attempt the read, and if this
    # user reads straight through the mode bit (root, or a filesystem that ignores
    # it) the answer would be the text — skip rather than assert the wrong way.
    override.write_text("at most {max_facts} facts\n", encoding="utf-8")
    override.chmod(0o000)
    try:
        try:
            override.read_text(encoding="utf-8")
        except PermissionError:
            pass
        else:
            pytest.skip("this user reads straight through mode 0o000")
        assert readable_override_text(override) is None
    finally:
        override.chmod(0o600)

    # A mode-0o000 PARENT directory answers None: stat and read are both blocked,
    # and `is_file()` itself is the raise the docstring names. Restore the parent
    # mode in `finally` — it runs even on the skip above — or the rest of this
    # file (and pytest's tmp cleanup) could not touch the tree at all.
    override.parent.chmod(0o000)
    try:
        try:
            override.read_text(encoding="utf-8")
        except PermissionError:
            pass
        else:
            pytest.skip("this user reads straight through mode 0o000")
        assert readable_override_text(override) is None
    finally:
        override.parent.chmod(0o700)
