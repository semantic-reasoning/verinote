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
    # The comparison fields need the contract to the same depth. "Use null for
    # fields that do not apply" was the vague line that let a model put an
    # operator on a lookup_object and kill the question.
    assert "Fill operator, value_type, and value only when kind is compare_typed_value" in text
    assert "leave all three null" in text


def test_query_intent_prompt_does_not_steer_threshold_questions_to_compare_typed_value():
    """The prompt must not ask for an intent `query_planner.py` cannot plan.

    `plan_query_candidates` has no `compare_typed_value` branch: it falls through
    to "unsupported intent kind" and the question lands in review_required. A
    prompt that told the model to answer threshold questions with
    compare_typed_value therefore made following the prompt *deterministically*
    fail. Until planner support lands, the prompt routes those to
    unknown_or_unsupported, which at least carries a reason a human can read.
    """
    text = default_prompt_text("query-intent")

    assert "worth more than 10 million" not in text
    assert "compare a typed value against a threshold" not in text
    assert (
        "Do not classify a question as compare_typed_value: threshold comparisons "
        "cannot be planned yet" in text
    )


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
