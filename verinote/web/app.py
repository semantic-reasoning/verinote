# SPDX-License-Identifier: MPL-2.0
"""The verinote web application.

Server-rendered with Jinja; interactivity via HTMX (the review toggle posts and
swaps a single row partial). No JS build step. The app owns one `Store` (SQLite).
"""

from __future__ import annotations

from importlib import resources
import inspect
import json
import logging
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
from threading import Lock
import unicodedata
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, nodes

from verinote.config import (
    APP_THEMES,
    MODEL_LISTING_PROVIDERS,
    PROVIDER_LABELS,
    PROVIDERS,
    TESTABLE_PROVIDERS,
    Config,
    ConfigCorruptError,
    CredentialsCorruptError,
    app_theme,
    _read_credentials,
    api_key_source,
    credentials_path,
    delete_credential,
    provider_key_env_var,
    save_credential,
    assert_credentials_intact,
    assert_settings_intact,
    normalize_provider,
    save_active_root,
    save_app_theme,
    save_settings,
)
from verinote.kb_location import KBLocationError, assert_kb_root_is_safe_to_create
from verinote.llm import MIN_REDACTABLE_SECRET, LLMError, get_client
from verinote.llm.base import ModelListing
from verinote.llm.claude_cli_adapter import CLI_MODEL_ALIASES
from verinote.llm.ollama_adapter import (
    OLLAMA_DEFAULT_BASE_URL,
    list_models as _list_ollama_models,
)
from verinote.llm.openrouter_adapter import (
    OPENROUTER_DEFAULT_BASE_URL,
    list_models as _list_openrouter_models,
)
from verinote.policy_defaults import DEFAULT_RELATION_ALIASES
from verinote.pipeline import (
    create_chunked_extraction_job,
    ExtractionJobBusyError,
    fact_trust_summary,
    IngestError,
    ingest_bytes,
    is_live_extraction_job,
    latest_source_job_ids,
    process_extraction_job,
    process_repair_job,
    store_source,
    supported_suffixes,
    translate_questions,
    verify,
    write_query_file,
)
from verinote.pipeline.policy_state import (
    PolicyMissingError,
    PolicyStatus,
    assert_writable,
    ensure_policy_marker,
    resolve_policy,
    write_default_policy,
)
from verinote.pipeline.query import load_query
from verinote.pipeline.question_outcome import question_outcome_view
from verinote.pipeline.ask import ask_question
from verinote.pipeline.acceptance import (
    accept_recommendations,
    accept_recommendations_for,
    apply_auto_accept_recommendations,
)
from verinote.pipeline.report_trace import ReportTrace, report_trace
from verinote.pipeline.corroboration import (
    canonical_relation,
    CorroborationPolicyError,
    merge_default_relation_aliases,
    normalize_typed_value,
    RELATION_ALIASES_RELPATH,
    relation_aliases,
    store_corroboration,
    store_relation_aliases,
    store_single_valued_conflicts,
    store_typed_relations,
    TYPED_RELATIONS_RELPATH,
)
from verinote.pipeline.workbench import trust_workbench
from verinote.prompts import (
    PromptError,
    delete_prompt_override,
    get_prompt,
    list_prompts,
    prompt_override_path,
    save_prompt_override,
)
from verinote.engine.terms import StringLit, render_term
from verinote.store import (
    DEFAULT_REVIEW_PAGE_SIZE,
    REVIEW_PAGE_SIZES,
    ReviewQueuePage,
    Store,
    TerminalFactError,
    engine_statuses,
    fact_status_order,
    is_actionable_fact_status,
    is_engine_input,
    review_statuses,
)
from verinote.store.duckdb_fact_terms import (
    DuckDBFactTermStoreError,
    DuckDBFactTermStoreLockedError,
)
from verinote.store.fact_input import nfc_term, structural_term, term_input_kind
from verinote.text import nfc

logger = logging.getLogger(__name__)

_TEMPLATES = resources.files("verinote.web").joinpath("templates")
_STATIC = resources.files("verinote.web").joinpath("static")

# What is served while a KB's recorded policy file is missing. Default-deny, and
# the allowlist is keyed by (method, path) rather than path alone: a page needed
# to *diagnose* the halt is not licence to *write* under the same prefix. The
# only writes allowed are the ones that leave this KB (switching root), because
# every other write — facts, and policy files like relation-aliases.md — would be
# a change made while this KB's rules are not being applied.
_POLICY_GUARD_READ_PATHS = ("/report", "/settings", "/static")
_POLICY_GUARD_WRITE_PATHS = ("/kb/select", "/settings/root")

# Providers whose Model field offers a *curated* list rather than a discovered
# one, keyed to the adapter constant so the two cannot drift: every suggestion
# here is an alias that adapter actually resolves. Deliberately not a set in
# `config` alongside MODEL_LISTING_PROVIDERS -- the values come from the adapter
# modules, which import `config`, so this is the layer that can name both.
MODEL_ALIASES_BY_PROVIDER = {"claudecli": CLI_MODEL_ALIASES}

# `list_models` is an interactive page-load call, not a generation, so it is
# bounded far tighter than `cfg.llm_timeout_seconds` (minutes, sized for a long
# local completion). It is provider-neutral on purpose: `_list_models_for`
# applies it to every lister, so it bounds any interactive model listing rather
# than Ollama's in particular, and it lives here because this dispatch is the
# last place that still holds a `Config` to clamp against. The effective bound
# stays the *smaller* of the two so a user who configures an even shorter
# timeout keeps it.
MODEL_LIST_TIMEOUT_SECONDS = 5.0

# How each listable provider's models are enumerated. Every lister takes
# `(base_url, timeout)` and NOTHING else — in particular never a `Config`.
#
# That signature is a security control, not a convention. `/settings/model-field`
# dials an endpoint the caller supplied in a query string, and `Config` carries a
# resolved `api_key`; worse, it is the key of the *saved* provider, because
# `Config.for_root` resolves it once and `dataclasses.replace(provider=...)` does
# not recompute it. Handing that object to a listing routine would mean one
# request could send, say, an Anthropic key to an attacker-named URL.
#
# What the check below buys, stated exactly, because an over-claim here is worse
# than no comment at all: nothing in the *shipped* table can be handed a key at
# the call site. Parameter names alone would not get that far — a
# `functools.partial(keyed_lister, cfg)`, a closure over a `Config`, and an
# object whose `__call__` takes `(base_url, timeout)` each present exactly the
# right parameters while carrying a key — so the check also demands a plain,
# non-closing function.
#
# Two things it does NOT buy, and neither is closed anywhere else:
#   1. It runs once at import, over the table as shipped — not at dispatch.
#      A test's `monkeypatch.setitem`, or any other runtime mutation of this
#      dict, is never seen by it; `_list_models_for` dispatches whatever it
#      finds there at call time.
#   2. It constrains what a lister is *handed*, never what its body can *reach*.
#      One line of `os.environ[...]` or `Config.for_root(...)` inside a lister
#      would put a key back within reach and still pass every clause.
#
# So this is a *weaker* control than the corrupt-config halt in `get_client`, and
# deliberately not the same argument (#269): that assert is the unavoidable first
# statement of the only construction path, which protects every caller by
# construction, whereas this is a shape rule evaluated once at import over one
# dict. The trade was accepted because dropping the `get_client` call is itself
# what puts the API key out of this seam's reach — the check exists to keep an
# ordinary-looking edit from quietly undoing that, not to make it unconditional.
#
# Every lister returns a `ModelListing`, not a bare list: a listing that reports
# which models advertise structured output and one that reports nothing but names
# have to arrive as the same type here, or this dispatch would need a per-provider
# branch to read its own table's results.
_MODEL_LISTERS = {
    "ollama": _list_ollama_models,
    "openrouter": _list_openrouter_models,
}
# The parameter names every lister must have, checked below rather than trusted.
_LISTER_SIGNATURE = ("base_url", "timeout")

# The URL each listable provider dials when the Base URL field is left blank,
# keyed to the adapter constant so the page cannot name one endpoint while the
# lister dials another. Deliberately not a mapping in `config`, for the reason
# `MODEL_ALIASES_BY_PROVIDER` is not either: the values live in the adapter
# modules, which import `config`, so this is the layer that can name both.
#
# It exists because the endpoint is *reported to the user*. Hardcoding
# `OLLAMA_DEFAULT_BASE_URL` here was correct while Ollama was the only listable
# provider and became a flat misreport the moment a second one joined — an
# OpenRouter user would have been told the page dialled `http://localhost:11434`.
_LISTABLE_DEFAULT_ENDPOINTS = {
    "ollama": OLLAMA_DEFAULT_BASE_URL,
    "openrouter": OPENROUTER_DEFAULT_BASE_URL,
}

# The providers a provider CHANGE clears the Base URL for (see `model_field`).
# A set named here rather than a literal at the decision site, so the rule can be
# checked against `MODEL_LISTING_PROVIDERS` at import: the clear renders a note
# naming the endpoint that would be dialled instead, and `_model_field_context`
# fills `endpoint` only for a listable provider. So a clearing provider that is
# not listable renders that sentence around an empty `<code></code>` — the same
# misreport `_check_every_listable_provider_names_its_default_endpoint` rejects a
# blank entry for, arriving through a door that check does not watch, since a
# one-line edit to either set opens it.
_BASE_URL_CLEARING_PROVIDERS = frozenset({"openrouter"})

# The Model field's copy is not shared prose: what a listing IS differs per
# provider (an installed set on a machine the user controls, versus a published
# catalogue read with no key), so the partial branches on the provider name and
# each arm asserts things true only of its own. The check that follows finds
# those arms by parsing the shipped template and walking its `if`/`elif` chains
# as syntax rather than matching its text, so what it counts as an arm is what
# Jinja will actually branch on. That is what lets it read the template instead
# of a list kept beside it: a list would be satisfied by appending a name, which
# is the edit the check exists to stop — and so, against a text scan, would
# writing that name into one of this file's comments.
_MODEL_FIELD_TEMPLATE = "partials/model_field.html"


def _check_every_listable_provider_has_a_keyless_lister(providers, listers) -> None:
    """Fail at import if the listing table and `MODEL_LISTING_PROVIDERS` disagree.

    Both directions matter and for different reasons. A provider in
    `MODEL_LISTING_PROVIDERS` with no lister would render a picker that can
    never fill; a lister for a provider not declared listable is dead code that
    a later edit could wire up without passing this gate.

    The shape clauses are the half that keeps "keyless" a fact about the shipped
    table rather than a claim in a comment. Each rejects a different way to smuggle
    a `Config` in: a `cfg` parameter (caught by the names), a `functools.partial`
    or a callable object that has already bound one (caught by `isfunction`), and
    a closure cell holding one (caught by `__closure__`). The clauses run in that
    order so `__closure__` is only read off something that has one. The names are
    read with `follow_wrapped=False` because `inspect.signature` otherwise reports
    the parameters of `__wrapped__`, which a lister that really takes a `cfg` can
    set to a two-parameter decoy. What none of them constrain is a body that
    *reaches* a key it was never handed, and none of them see the table after
    import — see the comment above.
    """
    missing = set(providers) ^ set(listers)
    if missing:
        raise RuntimeError(
            "every listable provider needs exactly one model lister and vice versa: "
            f"{sorted(missing)}"
        )
    not_plain = sorted(name for name, fn in listers.items() if not inspect.isfunction(fn))
    if not_plain:
        raise RuntimeError(
            "a model lister must be a plain function, not a functools.partial, a "
            "callable object, a bound method, or a builtin: each of those can already "
            f"hold a Config that its parameter names never mention: {not_plain}"
        )
    closing = sorted(name for name, fn in listers.items() if fn.__closure__ is not None)
    if closing:
        raise RuntimeError(
            "a model lister must not close over anything: a closure cell can hold a "
            f"Config that its parameter names never mention: {closing}"
        )
    wrong = sorted(
        name
        for name, fn in listers.items()
        if tuple(inspect.signature(fn, follow_wrapped=False).parameters) != _LISTER_SIGNATURE
    )
    if wrong:
        raise RuntimeError(
            f"a model lister must take exactly {_LISTER_SIGNATURE} so it cannot be "
            f"handed an API key: {wrong}"
        )


def _check_every_listable_provider_names_its_default_endpoint(providers, endpoints) -> None:
    """Fail at import if a listable provider has no default endpoint to name.

    A sibling of the lister check rather than another clause inside it, because
    it guards a different failure: not "the picker can never fill" but "the page
    names the wrong host".

    The two ways this map can be wrong fail differently, and the check is worth
    having for both. A *missing* entry is loud only where it bites: the
    subscript in `_model_field_context` sits behind `base_url.strip() or …`, so
    it raises `KeyError` and the route answers 500 only when the Base URL field
    is blank — a broken picker, not a misreport. A user who instead configured
    a proxy or gateway never reaches the subscript: the picker renders and
    names their own host correctly, and the missing entry stays invisible to
    exactly the users most likely to have a non-default endpoint. This check
    turns that split outcome into one import-time failure with a name in it,
    catching the entry before either half can happen. A *present but wrong*
    entry is the quiet one, and the misreport this map exists to prevent: the
    page renders and prints a URL that is not the one the lister dialled. No
    import-time check can tell a wrong URL from a right one, so what the map
    buys there is that each provider's URL is written once, beside the adapter
    constant it must equal.

    Both directions again, and the second is not cosmetic: an entry for a
    provider that is not listable is a default endpoint nothing dials, and the
    next edit to `MODEL_LISTING_PROVIDERS` would silently adopt it without anyone
    checking it is still right.

    A present-but-blank entry is rejected too: it satisfies membership and then
    renders as an empty `<code></code>` where the dialled host should be, which
    is the same misreport arriving quietly.

    Raised rather than asserted so `python -O` cannot strip it, matching
    `config._check_every_provider_is_classified`.
    """
    missing = set(providers) ^ set(endpoints)
    if missing:
        raise RuntimeError(
            "every listable provider needs exactly one default endpoint and vice versa: "
            "a missing entry raises KeyError on a model-field render with a blank "
            "Base URL, and is invisible on one with a Base URL set, and an entry no "
            "listable provider claims is a URL nothing dials "
            f"that the next edit would adopt unchecked: {sorted(missing)}"
        )
    blank = sorted(name for name, url in endpoints.items() if not (url or "").strip())
    if blank:
        raise RuntimeError(
            "a listable provider's default endpoint must be a URL the settings page "
            f"can name, not an empty string: {blank}"
        )


def _provider_names_compared(test) -> list[str]:
    """The literals one `if`/`elif` test compares `provider` against, in order.

    Every `provider == '<name>'` anywhere inside the test counts, not just a test
    that is exactly that comparison, so an arm guarded by `provider == 'x' and …`
    is still that provider's arm rather than a chain the walk fails to recognise.
    """
    found = []
    for node in (test, *test.find_all(nodes.Compare)):
        if not isinstance(node, nodes.Compare):
            continue
        if not (isinstance(node.expr, nodes.Name) and node.expr.name == "provider"):
            continue
        found.extend(
            op.expr.value
            for op in node.ops
            if op.op == "eq" and isinstance(op.expr, nodes.Const)
        )
    return found


def _model_field_provider_chains(template_source) -> list[tuple[int, list[str]]]:
    """Parse the template; return `(line, names)` per `if`/`elif` chain on `provider`.

    A chain is one `if` head plus the `elif` arms Jinja hangs off it, which is
    the unit the check reasons about: those arms are alternatives to each other,
    so a provider missing from one of them is a render that shows that provider
    nothing. Arms are collected off `elif_`; an `if` written inside another
    chain's body is a chain of its own rather than an arm of it. Nothing here
    cares whether a chain is a block or is written inline, so the four the
    template has — including the one inside an `<option>` tag — are all found.

    Chains whose tests never compare `provider` are not returned at all — the
    template's `{% if models %}` is not a place a provider needs an arm.
    """

    def arms(head):
        yield head
        for arm in head.elif_:
            yield from arms(arm)

    parsed = Environment().parse(template_source)
    all_ifs = list(parsed.find_all(nodes.If))
    continuations = {id(arm) for head in all_ifs for arm in head.elif_}
    chains = []
    for head in all_ifs:
        if id(head) in continuations:
            continue
        names = [name for arm in arms(head) for name in _provider_names_compared(arm.test)]
        if names:
            chains.append((head.lineno, names))
    return chains


def _check_every_listable_provider_has_model_field_copy(providers, template_source) -> None:
    """Fail at import if the Model field's copy does not branch per listable provider.

    The third sibling, guarding the third way this surface misreports. The
    lister check keeps the picker fillable and the endpoint check keeps the host
    name right; this one keeps the *sentences around them* attached to the
    provider they are true of. `{% if provider == 'ollama' %}…{% else %}…` was
    the shape here before, and it is the same mistake the hardcoded
    `OLLAMA_DEFAULT_BASE_URL` was: correct only while the set has exactly two
    members, and a flat misreport the moment a third joins — an else arm telling
    that third provider's users their endpoint "answered without an API key".

    So the shipped template is parsed, and every `if`/`elif` chain in it that
    compares `provider` to a literal must — on its own — give each listable
    provider exactly one arm and no other provider any. Per chain, not in total:
    totals are what the likely mistake slips past, because the template branches
    on the provider in four separate places and copy written at one chain and
    dropped from another leaves those renders showing that provider nothing
    while the totals still balance. Both directions at every chain, too. An
    unbranched listable provider has no copy there; an arm for a provider that is
    not listable is copy no render reaches, which the next edit to
    `MODEL_LISTING_PROVIDERS` would silently adopt without anyone re-reading it;
    a provider with two arms in one chain has a second one nothing can reach.
    At least one such chain has to exist, too: a per-chain rule has nothing left
    to fail on once every arm has been flattened back into shared prose.

    Parsed rather than matched over the file's text, and that is what makes "an
    arm" mean an arm. Jinja drops `{# … #}` in the lexer, so a name that appears
    only in the header comment above the markup is not seen here at all: it
    cannot stand in for an arm nobody wrote, and — the other direction, and the
    live one, since that comment already spends a paragraph explaining these
    chains — a comment that quotes one of these tests cannot fail the build, nor
    accuse a provider whose arms are all present.

    What it does NOT check, and cannot: whether the copy inside an arm is *true*
    of that provider. An arm could name the other provider's endpoint, or say
    "installed" about a published catalogue, and this would pass. Nor does it
    ask that per-provider copy live inside one of these chains — a sentence
    written outside every `provider` chain is a sentence this never sees. Like its
    siblings it is a shape rule over the shipped file, evaluated once at import.
    It forces a maintainer to write a sentence at each site it can see; a
    reviewer is what makes the sentence right.
    """
    location = f"verinote/web/templates/{_MODEL_FIELD_TEMPLATE}"
    chains = _model_field_provider_chains(template_source)
    if not chains:
        raise RuntimeError(
            f"{location} no longer branches on `provider` anywhere, so either its "
            "per-provider copy has become prose that claims one provider's listing is "
            "every provider's, or the arms moved somewhere this check cannot see them: "
            f"restore the `{{% if provider == '<name>' %}}` chains. Listable: "
            f"{sorted(providers)}"
        )
    faults = []
    for lineno, names in chains:
        missing = sorted(set(providers) - set(names))
        unlistable = sorted(set(names) - set(providers))
        repeated = sorted({name for name in names if names.count(name) > 1})
        if not (missing or unlistable or repeated):
            continue
        fault = f"the chain starting at line {lineno} has arms {names}"
        if missing:
            fault += f", missing {missing}"
        if unlistable:
            fault += f", arms for providers that are not listable {unlistable}"
        if repeated:
            fault += f", unreachable duplicate arms {repeated}"
        faults.append(fault)
    if faults:
        raise RuntimeError(
            "every `if`/`elif` chain that tests `provider` in "
            f"{location} must give each listable provider exactly one arm, and no "
            "other provider any, or those renders show that provider nothing at all. "
            "Where a chain below is missing one, add `{% elif provider == '<name>' %}` "
            "to that chain with copy that is true of <name> (what its listing "
            "enumerates, and what fixes a model missing from it); where it names an "
            "arm as unlistable or duplicated, delete that arm. Listable: "
            f"{sorted(providers)}. Wrong: "
            + "; ".join(faults)
        )


def _check_every_clearing_provider_can_name_its_replacement(clearing, listable) -> None:
    """Fail at import if a Base-URL-clearing provider has no endpoint to name.

    The fourth sibling, and the subset relation is the whole of it: every
    provider that clears the Base URL must be one whose models are listable.
    Not because the clear needs a listing, but because of what the clear must
    SAY. Taking the field away means the next Save stores no Base URL, so the
    note has to name what verinote would dial instead — and `endpoint` is filled
    by `_model_field_context` only for a provider in `MODEL_LISTING_PROVIDERS`.
    A clearing provider outside that set renders the sentence "verinote would
    dial <label>'s own <code></code> instead": a promise about the endpoint with
    the endpoint missing, which is the misreport the blank-entry clause of
    `_check_every_listable_provider_names_its_default_endpoint` already rejects,
    reached here by a different door — a one-line edit to either set.

    One direction only, unlike its siblings. A listable provider that does NOT
    clear is the normal case and the one Ollama is: clearing on every switch
    would wipe an endpoint the user typed. So this must stay a subset test, never
    a symmetric difference.

    Raised rather than asserted so `python -O` cannot strip it, matching its
    siblings.
    """
    stray = sorted(set(clearing) - set(listable))
    if stray:
        raise RuntimeError(
            "a provider that clears the Base URL must be one whose models are "
            "listable, because the note the clear renders names the default "
            "endpoint that would be dialled instead and only a listable provider "
            f"has one to name: {stray}"
        )


# The clearing check goes first: the only edit that can reach it while the three
# below still pass is one to `_BASE_URL_CLEARING_PROVIDERS`, and the one that can
# reach it while they do NOT — narrowing `MODEL_LISTING_PROVIDERS` — has to be
# seen here before the table checks reject the same narrowing under their own
# names. That ordering is also what lets a test prove this call site exists.
#
# Invariant these four call sites hold, and how to re-establish it: deleting any
# one call site, on its own, must make exactly one module-body test fail — the
# one below that pins THAT guard (`test_the_module_body_runs_the_clearing_provider_check`,
# `test_the_module_body_runs_the_check` for the keyless-lister guard,
# `test_the_module_body_runs_the_default_endpoint_check`,
# `test_the_module_body_runs_the_model_field_copy_check`). This is not
# self-enforcing — nothing here runs that matrix automatically — so it is only
# as true as the last time someone ran it by hand. It failed silently once
# already: a sibling guard raised on the same narrowed fixture the lister
# check's test used, on a message that happened to name the same provider, so
# the lister test kept passing for the wrong reason for two commits. Adding a
# guard, deleting one, or reordering these calls means re-running the
# delete-one-at-a-time matrix by hand, because a sibling raising on the same
# fixture can silently make a neighbour's test vacuous.
_check_every_clearing_provider_can_name_its_replacement(
    _BASE_URL_CLEARING_PROVIDERS, MODEL_LISTING_PROVIDERS
)
_check_every_listable_provider_has_a_keyless_lister(MODEL_LISTING_PROVIDERS, _MODEL_LISTERS)
_check_every_listable_provider_names_its_default_endpoint(
    MODEL_LISTING_PROVIDERS, _LISTABLE_DEFAULT_ENDPOINTS
)
_check_every_listable_provider_has_model_field_copy(
    MODEL_LISTING_PROVIDERS,
    _TEMPLATES.joinpath(_MODEL_FIELD_TEMPLATE).read_text(encoding="utf-8"),
)


def _list_models_for(cfg: Config, provider: str, base_url: str | None) -> ModelListing:
    """Enumerate `provider`'s models at `base_url`, handing the lister no key.

    This must remain the only place a lister from `_MODEL_LISTERS` is called.
    Nothing enforces that; it is an invariant maintainers have to uphold, and
    the safety argument rests on it — a second call site could reach a lister
    without the two halts below, so a lister added later would be dialled
    unguarded.

    Those halts are hand-written per-route: exactly the enumeration #269 was
    able to avoid, because it centralised both asserts in `get_client` as that
    function's first act, protecting every caller by construction. That cost is
    accepted here because dropping the factory call is precisely what puts the
    API key out of the listing seam's reach. A corrupt config.json or an
    unreadable credentials file must still halt before any provider is dialled.

    Settings before credentials, matching `get_client`'s order -- this seam
    hand-writes what that function centralises, so nothing besides this
    comment and a test keeps the two surfaces agreeing. The order is
    observable, not academic: a corrupt config.json falls back to the
    built-in cloud default, which requires a key, so `credentials_error` can
    be set on the very same `Config` that carries `settings_error`. Config
    wins that race because it is the more fundamental failure -- the
    provider itself could not be resolved, so a credentials verdict for
    whatever provider *would* have been resolved is not yet meaningful.
    Swapping these two lines produces zero test failures anywhere else in
    the suite; see `test_model_field_halts_on_config_first_when_both_errors_are_set`.
    """
    assert_settings_intact(cfg)
    assert_credentials_intact(cfg)
    return _MODEL_LISTERS[provider](
        base_url, min(cfg.llm_timeout_seconds, MODEL_LIST_TIMEOUT_SECONDS)
    )


# Full-page halt shown when a fact's logical terms cannot be read. htmx partial
# swaps are redirected here (HX-Redirect) because htmx will not swap an error
# response into the DOM -- see `_fact_terms_unreadable_handler`.
FACT_TERMS_UNAVAILABLE_PATH = "/fact-terms-unavailable"

# Full-page halt shown when this KB's config.json is present but corrupt. Same
# HX-Redirect treatment as the fact-terms halt for the same htmx reason -- see
# `_config_corrupt_handler`.
CONFIG_UNAVAILABLE_PATH = "/config-unavailable"

# Full-page halt shown when the machine-wide credentials file is present but
# unreadable. A distinct path and page from the config.json halt on purpose: a
# different file, a different scope (every KB, not one), and a different fix.
CREDENTIALS_UNAVAILABLE_PATH = "/credentials-unavailable"

# The public action names are deliberately separate from the append-only audit
# vocabulary. A halt redirect carries both forms through SQLite validation.
_FACT_DECISION_LOG_ACTIONS = {
    "toggle": "toggled",
    "accept": "accepted",
    "reject": "rejected",
}


# A browser attaches ambient authority to requests a *different* site provokes:
# verinote listens on a fixed loopback port, has no auth, and a urlencoded form
# POST is a CORS "simple" request, so any page the user visits can drive one. The
# concrete reach today is `POST /settings` (which stores an unvalidated base_url)
# followed by `POST /settings/test` (which dials it carrying the API key).
#
# This is a browser-ambient-authority gate, NOT an authorization mechanism: it is
# deliberately fail-open for clients that send neither header, because no browser
# page can produce that combination on a state-changing request (see below).
_SAFE_FETCH_SITES = frozenset({"same-origin", "none"})
# Reads are not gated: no CORS header is set anywhere, so a cross-origin GET is
# issued but unreadable, and gating navigation would 403 an ordinary inbound
# link. The exception is keyed on "does this dial a caller-supplied endpoint",
# not on "does this mutate" — `/settings/model-field` takes `base_url` from the
# query string and dials it. `_MODEL_LISTERS` is what keeps that a blind SSRF
# probe rather than key exfiltration, by handing the listing seam no key and
# building no client that holds one — narrower than "no way to hold a key",
# since an import-time shape rule cannot stop a lister body from going and
# fetching one (see `_MODEL_LISTERS`). This guard is the separate, weaker
# control that stops another site from provoking the probe at all. Neither
# substitutes for the other, which is why this path stays listed even though
# the listing is handed no key.
_ORIGIN_GUARD_GET_PATHS = ("/settings/model-field",)


def _matches(path: str, allowed: tuple[str, ...]) -> bool:
    return any(path == a or path.startswith(a + "/") for a in allowed)


def _origin_guard_applies(method: str, path: str) -> bool:
    if method not in {"GET", "HEAD", "OPTIONS"}:
        return True
    return _matches(path, _ORIGIN_GUARD_GET_PATHS)


def _is_same_origin(request: Request) -> bool:
    """Whether a request's declared origin matches the authority it was sent to.

    Deliberately NOT "attributable to verinote's own UI": this does not survive
    DNS rebinding. A page on a domain the attacker rebinds to loopback is
    genuinely same-origin with the service, so `Origin` and `Host` agree and this
    returns True. Blocking that needs the request authority checked against the
    bind address, which this process cannot see (uvicorn is handed `--host` by
    the CLI) — a separate change, and one that would refuse every non-loopback
    deployment. What this does close is the ordinary cross-origin case, where an
    attacker's page keeps its own origin and cannot forge these headers.

    `Origin` is authoritative when present: the Fetch spec attaches it to every
    request whose method is not GET/HEAD regardless of CORS mode — which is
    exactly why a form POST needs no preflight and *still* carries it — and no
    HTML or JS API lets a page suppress it. Compared against `Host` rather than a
    hardcoded loopback address because the port is configurable and the app
    cannot see uvicorn's bind address; host:port only, since a TLS-terminating
    proxy legitimately changes the scheme.

    Falling back to `Sec-Fetch-Site` covers same-origin POSTs from browsers that
    omit `Origin`. `same-site` is refused along with `cross-site`: for an IP host
    every loopback port is same-site but cross-origin, so accepting it would let
    any other local dev server drive verinote.

    Both absent means the caller is not a browser page this project supports —
    curl, a script, the test client. Such a caller carries no ambient authority
    to abuse, so it is allowed. Note the two paths are not equally defended: a
    POST always carries `Origin`, so it has two independent signals, whereas a
    cross-origin GET (`<img>`, `<script>`, a `form method=get`) never carries one
    at all, leaving the gated GET below resting entirely on `Sec-Fetch-Site`.
    """
    origin = request.headers.get("origin")
    if origin is not None:
        netloc = urlsplit(origin).netloc
        # `Origin: null` (sandboxed iframe, opaque origin) has no netloc, and
        # must not be allowed to match an empty or missing Host header.
        return bool(netloc) and netloc.lower() == (request.headers.get("host") or "").lower()
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site in _SAFE_FETCH_SITES
    return True


def _policy_guard_exempt(method: str, path: str) -> bool:
    if method in {"GET", "HEAD", "OPTIONS"}:
        return _matches(path, _POLICY_GUARD_READ_PATHS)
    return path in _POLICY_GUARD_WRITE_PATHS


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg if cfg is not None else Config.load_for_ui()

    app = FastAPI(title="verinote")
    app.state.cfg = cfg
    app.state.store = None
    app.state.repair_scheduler_lock = Lock()
    app.state.repair_scheduled = set()
    if cfg is not None:
        assert_kb_root_is_safe_to_create(cfg.root)
        store = Store(cfg.db_path)
        store.init_schema()
        ensure_policy_marker(store, cfg.root)
        app.state.store = store

    def _common_template_context(request: Request) -> dict[str, int | str]:
        """Values needed by shared templates, including the primary navigation."""
        store = request.app.state.store
        if store is None:
            return {"review_count": 0, "theme": app_theme()}

        status_counts = store.status_counts()
        return {
            "review_count": sum(
                status_counts.get(status, 0) for status in review_statuses()
            ),
            "theme": app_theme(),
        }

    templates = Jinja2Templates(
        directory=str(_TEMPLATES),
        context_processors=[_common_template_context],
    )
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    def _policy_halted(request: Request, message: str):
        return templates.TemplateResponse(
            request,
            "policy_halted.html",
            {"message": message},
            status_code=409,
        )

    @app.middleware("http")
    async def policy_halted_guard(request: Request, call_next):
        """Fail closed while this KB's recorded logic policy is missing.

        A halted KB must not be *written* to either: an accept/reject decision
        taken while the KB's rules are not being applied is a fake review gate,
        and SQLite autocommits the status change long before rendering fails. So
        the guard runs before the route does, and only the recovery paths pass.
        """
        store = app.state.store
        if store is not None and not _policy_guard_exempt(request.method, request.url.path):
            # Same predicate the CLI dispatch and the extraction worker use, so the
            # three enforcement points cannot disagree about what "halted" means.
            try:
                assert_writable(store)
            except PolicyMissingError as exc:
                return _policy_halted(request, str(exc))
        return await call_next(request)

    # Defined AFTER `policy_halted_guard` so it runs BEFORE it: `add_middleware`
    # inserts at index 0, so the last one defined is the outermost. That
    # inversion is easy to undo by reordering, so it is asserted by a test.
    #
    # Outermost is the point. An unattributable request must not reach the policy
    # guard, which reads the store and renders a templated 409 — that would be an
    # attacker-triggerable oracle for the KB's policy state. It also keeps this
    # guard's exemptions exactly its own, so another guard's allowlist can never
    # silently widen this one.
    @app.middleware("http")
    async def same_origin_guard(request: Request, call_next):
        if _origin_guard_applies(request.method, request.url.path) and not _is_same_origin(
            request
        ):
            logger.warning(
                "refused cross-origin %s %s (origin=%r)",
                request.method,
                request.url.path,
                request.headers.get("origin"),
            )
            # Plain text, not a template: rendering one runs the shared context
            # processor, which reads the store for a request just judged
            # untrustworthy. 403 rather than this app's 409 halt vocabulary, so a
            # forged request is never mistaken for a KB halt. No HX-Redirect —
            # that pattern exists so a *user's* htmx action cannot silently
            # no-op, and this response is delivered to the attacker's document,
            # not the user's tab.
            return Response(
                "cross-origin request refused\n",
                status_code=403,
                media_type="text/plain",
            )
        return await call_next(request)

    def _policy_missing_handler(request: Request, exc: Exception):
        """Backstop for *display* only — it cannot prevent a write.

        An exception handler runs after the route body has already run, so a
        route that writes before it reads the policy would commit (SQLite
        autocommits) and still render this page: "looks rejected, actually
        written". Write blocking is the middleware's default-deny above; this
        only guarantees that a route added outside the guard shows the loud page
        instead of a stack trace.
        """
        return _policy_halted(request, str(exc))

    app.add_exception_handler(PolicyMissingError, _policy_missing_handler)

    def _fact_terms_unavailable_page(
        request: Request, *, saved_decision: str | None = None
    ):
        # Deliberately generic: DuckDBFactTermStoreError covers a corrupt/unopenable
        # sidecar but also stale/missing-term and malformed-input conditions, so the
        # copy must not diagnose one specific cause it cannot be sure of.
        return templates.TemplateResponse(
            request,
            "sidecar_unreadable.html",
            {"saved_decision": saved_decision},
            status_code=409,
        )

    @app.get(FACT_TERMS_UNAVAILABLE_PATH, response_class=HTMLResponse)
    def fact_terms_unavailable(
        request: Request,
        decision_fact_id: str | None = None,
        decision_action: str | None = None,
        decision_log_id: str | None = None,
    ):
        # The full-page halt and the HX-Redirect target below. It reads no fact
        # terms, so it still renders while the term store cannot be read.
        saved_decision = _saved_fact_decision(
            decision_fact_id, decision_action, decision_log_id
        )
        return _fact_terms_unavailable_page(request, saved_decision=saved_decision)

    def _saved_fact_decision(
        fact_id: object, action: object, log_id: object
    ) -> str | None:
        """Validate a halt notice against the durable SQLite decision audit.

        The URL is deliberately self-contained so it survives an application
        restart. It is not trusted: the referenced audit row must be the fact's
        latest human decision and the current SQLite status must still be the
        status that action committed. This keeps forged and stale URLs generic.
        """
        if not isinstance(action, str) or action not in _FACT_DECISION_LOG_ACTIONS:
            return None
        try:
            parsed_fact_id = int(fact_id)
            parsed_log_id = int(log_id)
        except (TypeError, ValueError):
            return None
        if parsed_fact_id <= 0 or parsed_log_id <= 0:
            return None

        store = app.state.store
        if store is None:
            return None
        fact = store.get_fact(parsed_fact_id)
        log = store.fact_log(parsed_fact_id)
        if fact is None or not log:
            return None
        latest = log[-1]
        if (
            int(latest["id"]) != parsed_log_id
            or latest["action"] != _FACT_DECISION_LOG_ACTIONS[action]
        ):
            return None
        matching_events = [
            event
            for event in store.fact_events(parsed_fact_id)
            if event["event_type"] == _FACT_DECISION_LOG_ACTIONS[action]
            and event["actor"] == "human"
        ]
        if not matching_events:
            return None
        try:
            after = json.loads(matching_events[-1]["after_json"])
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(after, dict) or after.get("status") != fact["status"]:
            return None
        return action

    def _fact_terms_unreadable_handler(request: Request, exc: Exception):
        """One loud, non-lying halt for every surface that cannot read a fact's
        logical terms.

        Read routes (`/review`, `/provenance`, `GET /facts/{id}/edit`, `/report`'s
        trace) let `DuckDBFactTermStoreError` propagate here rather than degrading
        a structural fact to a silent `kind="string"`. One narrow exception since
        #311: `GET /facts/{id}/edit` on a *superseded* fact returns the read-only
        row without building the edit context, so it does not read that fact's
        terms — the row render can still raise for its own reasons, but the edit
        form's read is skipped. What reaching this handler
        means on a POST is route-dependent: `amend_fact` refuses in the store
        *before* it commits, so nothing was written; but `accept`/`reject`/`toggle`
        do a bare SQLite status UPDATE that autocommits immediately and only reach
        this handler on the follow-on row re-render, so for those the decision
        already succeeded and merely could not be displayed. The decision routes
        mark only a changed, committed decision on the request. That explicit
        state adds a saved-decision notice here; all other halts stay generic.

        htmx will NOT swap a 4xx/5xx response into the DOM -- it fires
        `htmx:responseError` and swaps nothing -- so answering an htmx partial
        swap (the edit form, the amend save) with an inline page would be a
        *silent* no-op, the exact failure #173 forbids. For htmx requests we send
        HX-Redirect to force a full-page navigation to the halt page; htmx 2.x
        acts on HX-Redirect regardless of status, so the 409 stays honest.
        Full-page (non-htmx) requests render the halt page inline.
        """
        saved_decision = _saved_fact_decision(
            *getattr(request.state, "saved_fact_decision", (None, None, None))
        )
        if request.headers.get("HX-Request") == "true":
            redirect_path = FACT_TERMS_UNAVAILABLE_PATH
            notice = getattr(request.state, "saved_fact_decision", None)
            if saved_decision and notice is not None:
                redirect_path += "?" + urlencode(
                    {
                        "decision_fact_id": notice[0],
                        "decision_action": notice[1],
                        "decision_log_id": notice[2],
                    }
                )
            return Response(
                status_code=409,
                headers={"HX-Redirect": redirect_path},
            )
        return _fact_terms_unavailable_page(request, saved_decision=saved_decision)

    app.add_exception_handler(
        DuckDBFactTermStoreError, _fact_terms_unreadable_handler
    )

    def _config_corrupt_page(request: Request, reason: str | None):
        return templates.TemplateResponse(
            request,
            "config_corrupt.html",
            {"reason": reason},
            status_code=409,
        )

    @app.get(CONFIG_UNAVAILABLE_PATH, response_class=HTMLResponse)
    def config_unavailable(request: Request):
        # The full-page halt and the HX-Redirect target below. It reads no config
        # file, so it still renders while config.json is corrupt; the already
        # resolved `settings_error` field is shown for context when present.
        cfg = app.state.cfg
        return _config_corrupt_page(request, cfg.settings_error if cfg else None)

    def _config_corrupt_handler(request: Request, exc: Exception):
        """One loud, non-lying halt for every surface that would reach a provider
        under a corrupt config.json.

        Refusing here rather than silently resolving to the cloud default is the
        whole point (#269): a user who chose `ollama` must not have a corrupt
        config quietly ship their next extraction to `anthropic`.

        htmx will NOT swap a 4xx/5xx response into the DOM -- it fires
        `htmx:responseError` and swaps nothing -- so answering an htmx request
        with an inline page would be a *silent* no-op, the failure #173 forbids.
        For htmx requests we send HX-Redirect to force a full-page navigation to
        the halt page; full-page requests render it inline at 409.
        """
        if request.headers.get("HX-Request") == "true":
            return Response(
                status_code=409,
                headers={"HX-Redirect": CONFIG_UNAVAILABLE_PATH},
            )
        return _config_corrupt_page(request, str(exc))

    app.add_exception_handler(ConfigCorruptError, _config_corrupt_handler)

    def _credentials_corrupt_page(request: Request, reason: str | None):
        cfg = app.state.cfg
        provider = cfg.provider if cfg else ""
        return templates.TemplateResponse(
            request,
            "credentials_corrupt.html",
            {
                "reason": reason,
                "credentials_path": str(credentials_path()),
                "env_var": provider_key_env_var(provider) if provider else "VERINOTE_API_KEY",
            },
            status_code=409,
        )

    @app.get(CREDENTIALS_UNAVAILABLE_PATH, response_class=HTMLResponse)
    def credentials_unavailable(request: Request):
        cfg = app.state.cfg
        return _credentials_corrupt_page(request, cfg.credentials_error if cfg else None)

    def _credentials_corrupt_handler(request: Request, exc: Exception):
        """Same htmx reasoning as the config halt (#173): a 4xx is never swapped,
        so an inline body would be a silent no-op."""
        if request.headers.get("HX-Request") == "true":
            return Response(
                status_code=409,
                headers={"HX-Redirect": CREDENTIALS_UNAVAILABLE_PATH},
            )
        return _credentials_corrupt_page(request, str(exc))

    app.add_exception_handler(CredentialsCorruptError, _credentials_corrupt_handler)

    def _active_store() -> Store:
        store = app.state.store
        if store is None:
            raise RuntimeError("no active KB")
        return store

    def _active_cfg() -> Config:
        cfg = app.state.cfg
        if cfg is None:
            raise RuntimeError("no active KB")
        return cfg

    def _error_cause(exc: BaseException) -> str:
        """The exception's own message, or its type name when that message is blank."""
        text = str(exc)
        return text if text.strip() else type(exc).__name__

    def _short_error(exc: BaseException) -> str:
        return " ".join(_error_cause(exc).split())[:240]

    def _fail_pending_translations(store: Store, cfg: Config, exc: LLMError) -> None:
        # Not `_short_error`: this reason is a standalone column, not interpolated
        # into a separator, and `question_outcome_view` renders a per-status
        # sentence when it is blank. Substituting a type name here replaces that
        # sentence, it does not rescue a dangling colon.
        reason = " ".join(str(exc).split())[:240]
        for q in store.questions(pending_only=True):
            store.set_question_query(q["id"], None, "translation_failed", reason)
        write_query_file(store, cfg.root)

    def _extraction_schema_hint(cfg: Config) -> str:
        try:
            return cfg.extraction_schema_hint()
        except PromptError as exc:
            raise LLMError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - normalise every render failure
            # This call is an argument expression inside the extraction worker's
            # `try`, so without this clause an unreadable override lands on the
            # generic handler and the job says `analysis failed` — a diagnosis of
            # the provider for a file on this machine. Name the prompt:
            # `str(UnicodeDecodeError)` names no file at all.
            raise LLMError(
                f"prompt extraction-limit-hint could not be loaded: {exc}"
            ) from exc

    def _relation_aliases_path() -> Path:
        return _active_cfg().root / RELATION_ALIASES_RELPATH

    def _policy_file_failure(read, relpath: str) -> str | None:
        """Normalise ONE trust-policy file read into a message, or None.

        Shared by both legs of `_trust_policy_failure` because the two files
        fail the same two ways and must be reported the same two ways. `read` is
        a zero-argument callable so the try wraps EXACTLY ONE CALL, not a route
        body, and that call touches no database: `store_relation_aliases` and
        `store_typed_relations` each read `store.db_path` as an attribute, stat
        their file, and parse it. So the only thing this can swallow is a
        failure to read or parse that one file.
        """
        try:
            read()
        except CorroborationPolicyError as exc:
            # G1. Already normalised, and its message already begins with the
            # file's own name (`relation-aliases.md:1: expected …`,
            # `typed-relations.md: alias 'x' used for both …`). Prefixing it
            # below would say the file twice, AND would misstate what happened —
            # the file WAS read; it parsed and failed. That is a false claim
            # about the system's own state on a user-facing page. Must stay
            # ABOVE G2.
            return str(exc)
        except Exception as exc:  # noqa: BLE001 - normalise every policy-read failure
            # G2. BROAD, NOT A TYPE LIST. `UnicodeDecodeError` (a file saved as
            # cp949) descends from `ValueError` as `CorroborationPolicyError`
            # does but is no subclass of it; `PermissionError` is not a
            # `ValueError` at all. Same reasoning, and the same shape, as
            # `extract.py::_relation_aliases_or_error` (#553).
            #
            # NAME THE FILE: `str(UnicodeDecodeError)` is a byte offset and no
            # path, so an unprefixed message put a bare codec complaint about
            # nothing in particular on the page.
            return f"{relpath} could not be read: {exc}"
        return None

    def _trust_policy_failure(store: Store) -> str | None:
        """Why this KB's trust policy cannot be applied, or None when it can.

        TWO FILES, ONE SIGNAL. `fact_trust_summary`, `_source_trust_rollup`,
        `trust_workbench` and `store_single_valued_conflicts` each read BOTH
        `policy/relation-aliases.md` and `policy/typed-relations.md`, and either
        one being unreadable 500s them identically. (`store_corroboration` is
        the one trust function that reads only the alias file — which is why
        this is a guard on the callers, not a claim that every trust-derived
        value depends on both files.) The question every caller asks is the same
        ("can this KB's trust policy be applied?") and the consequence is the
        same (withhold every trust-derived value), so it is one string rather
        than two flags. Each parser's message already names its own file, so the
        returned string carries the attribution without a second value to
        thread (#585).

        ORDER, AND WHAT THE MESSAGE DOES NOT SAY. Aliases are checked first,
        typed relations second, and the FIRST failure is what comes back. So
        when both files are broken the page names the alias file and says
        nothing at all about the typed one — not that it is healthy. Callers
        and templates must not turn "the message names one file" into "the
        other file is fine": repairing the named file can surface a second
        failure on a page that had been answering 200. That is not a defect
        being hidden, it is the only ordering available without reading a file
        the first failure already made irrelevant, and it is why no banner this
        value feeds claims any file is readable.

        WHY THIS GUARD CARRIES ITS OWN MARKER RATHER THAN CHECKING THE VALUE.
        On the alias side, degrading means computing under
        `DEFAULT_RELATION_ALIASES` — a value the user demonstrably does not
        have, because the delta from the defaults is the only reason their file
        exists. There is no `DEFAULT_TYPED_RELATIONS`: `store_typed_relations`
        returns `{}` when the file is absent, which is the normal state of most
        KBs. So a typed failure degraded to `{}` renders BYTE-IDENTICALLY to a
        healthy KB that declares no typed relations, and no assertion on the
        rendered value could tell them apart. The withheld state has to be
        signalled by this value being non-None, never inferred from the numbers
        (#585).

        Both calls resolve their file as `store.db_path.parent / <relpath>`,
        which agrees with `_relation_aliases_path()` (`_active_cfg().root / …`)
        for every `Config.for_root`, but the two are independent `Config`
        fields, so a guard that re-derived the path could clear one file while
        the route read the other and still 500 (#555). Calling the pipeline
        functions the routes' own failures come out of is what keeps them
        agreeing.

        These reads happen chronologically FIRST (measured: they run before any
        pipeline function does, since the route calls this before computing
        anything policy-dependent) — but they are not the ONLY reads. When the
        files are healthy, the route's own pipeline functions
        (`store_corroboration`, `trust_workbench`, `fact_trust_summary`, …) read
        both files again themselves; this call does not replace those reads or
        cache the result for them, and it is not a fix for the check-then-use
        gap that leaves: a rewrite between this call and the route's own reads
        still 500s. Freezing one read and threading it through every
        policy-dependent pipeline function would change signatures shared with
        the CLI, and is a separate refactor.

        NOT EVERY POLICY-DEPENDENT ROUTE CALLS THIS. `POST /ask` and
        `POST /questions/translate` fail for BOTH files inside
        `build_query_schema_snapshot` (`query_schema.py`), which
        `verinote/cli.py` reaches too, and they have no guard here for either
        file. `POST /ask` is therefore also the one route where the ordering
        rule above has nothing upstream of it: everywhere else a tripped guard
        returns before the typed read can run at all (measured). Both are
        deferred to #570's follow-up, which already owes their alias half.
        """
        alias_failure = _policy_file_failure(
            lambda: store_relation_aliases(store), RELATION_ALIASES_RELPATH
        )
        if alias_failure is not None:
            return alias_failure
        # Explicit `is not None` rather than `or`: an exception with no args
        # stringifies to "", which is falsy, and an `or` chain would silently
        # step past a real alias failure to read a file the user cannot act on
        # yet.
        return _policy_file_failure(
            lambda: store_typed_relations(store), TYPED_RELATIONS_RELPATH
        )

    def _relation_aliases_context() -> dict[str, object]:
        """The Settings page's own read of the alias file, plus its own guard.

        Deliberately NOT `_trust_policy_failure` + `store_relation_aliases`:
        this route never calls `store_relation_aliases` (it reads the file
        straight from `_relation_aliases_path()`, which resolves from
        `cfg.root` rather than `store.db_path.parent` — see
        `_trust_policy_failure`'s docstring), so it needs its own read and its
        own broad `except Exception` around that one `read_text` call (same G2
        rationale: no database access, only a stat and a read of this file).
        """
        path = _relation_aliases_path()
        if not path.is_file():
            return {
                "relation_aliases": DEFAULT_RELATION_ALIASES,
                "relation_aliases_error": None,
                "relation_aliases_unreadable": False,
            }
        try:
            text = path.read_text(encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - normalise every alias-read failure
            # BROAD, not just `UnicodeDecodeError`: `PermissionError`,
            # `IsADirectoryError`, and other `OSError`s reach this too (measured,
            # #555 BLOCKER-3), so the flag and the message say "read", never
            # "decoded" — "decoded" was true only for the one mode among several
            # this clause actually catches, and was a false claim about the
            # system's own state for the others (e.g. a `chmod 000` file).
            return {
                "relation_aliases": "",
                "relation_aliases_error": f"{RELATION_ALIASES_RELPATH} could not be read: {exc}",
                "relation_aliases_unreadable": True,
            }
        try:
            existing = relation_aliases(text)
        except CorroborationPolicyError as exc:
            # The file WAS read; it parsed and failed. Keep the parser's own
            # message (already file-and-line-prefixed) rather than wrapping it —
            # wrapping would say the file twice and misstate that it could not be
            # read at all (same reasoning as G1 above).
            return {
                "relation_aliases": text,
                "relation_aliases_error": str(exc),
                "relation_aliases_unreadable": False,
            }
        merged = merge_default_relation_aliases(existing)
        missing_defaults = {
            alias: canonical
            for alias, canonical in merged.items()
            if alias not in existing
        }
        if not missing_defaults:
            return {
                "relation_aliases": text,
                "relation_aliases_error": None,
                "relation_aliases_unreadable": False,
            }
        missing_text = "\n".join(
            f"- `{alias}` -> `{canonical}`"
            for alias, canonical in sorted(missing_defaults.items())
        )
        return {
            "relation_aliases": (
                f"{text.rstrip()}\n\n# Default aliases not yet saved in this KB\n{missing_text}\n"
            ),
            "relation_aliases_error": None,
            "relation_aliases_unreadable": False,
        }

    def _write_relation_aliases_atomic(path: Path, text: str) -> None:
        """Replace `path` with `text`, or leave it exactly as it was.

        Follows `config.py::_write_json_atomic`'s shape — mkstemp beside the
        target, `fsync` before `os.replace` — rather than
        `query.py::write_query_file`'s fuller one (which also fsyncs the
        parent directory and threads a `BaseException` handler around an
        `os.fdopen`-wrapped fd). These are two INDEPENDENT differences, not one
        choice: the parent-directory fsync is decided below, on durability
        grounds. The fd-ownership handling is not a separate decision here —
        this function inherits `_write_json_atomic`'s exact shape for it,
        `fd`/`tmp` included, so it has the same gap `query.py` closes and
        `config.py` does not: if `os.fchmod` or `os.fdopen` itself raises
        (before the `with` block owns the fd), the raw `fd` from `mkstemp` is
        never explicitly closed — `tmp` is still unlinked below regardless,
        and the process still reclaims the fd at exit, but it is a real,
        measured leak in the interim (#555 gate rev-6), not a durability
        question at all.

        PARENT-DIRECTORY FSYNC, deliberately skipped. Not explaining WHY
        `write_query_file` fsyncs its parent and this one doesn't —
        `query.py` never says why it needs that stronger guarantee, and two
        earlier rounds of this docstring each guessed a different reason and
        were each wrong ("paired with a database commit"; "a snapshot it must
        not outlive" — neither is stated anywhere in `query.py`). The fact
        both guesses were reaching for does hold, checked directly rather
        than reasoned from either: that transaction commits no database
        write, on any path reachable in the tree today, including through its
        one caller-supplied guard — and that enumeration is CLOSED, not just
        long: `Store.immediate_transaction` has exactly one definition and no
        subclass overrides it anywhere in the tree. The derivation is not
        repeated here — three rounds running, it has been the part that goes
        stale, not the one-line result. What IS checkable: this route
        publishes one file and returns a redirect, so the guarantee bought by
        `fsync`-before-`replace` alone ("never torn", not
        "committed before we return" — `_write_json_atomic`'s own phrase) is
        complete for what it does. `write_query_file` makes a strictly
        stronger guarantee than that; this docstring does not claim to know
        why.

        `write_text` (the prior implementation) truncates in place, so a write
        that fails partway through — full disk, a killed process, two
        processes saving at once — can leave the file empty or half-written.
        Measured end-to-end on a full filesystem (#555, gate REV-3): the alias
        file was left truncated to `b""` while the page reported "Nothing was
        saved", which was then false. `mkstemp` + `os.replace` means a failed
        write leaves `path` untouched — the reader (`_relation_aliases_path()`
        callers) always sees either the whole old file or the whole new one.

        MODE. Uses a FIXED `0o644`, like `_write_json_atomic` uses a fixed
        mode, rather than deriving one from the existing file the way
        `write_query_file` does — deliberately NOT reused here even though
        this route replaces an existing file more often than `write_query_file`
        does. `write_query_file` derives its mode to preserve a KB's chosen
        permissions across regenerations of a machine-written file. This
        route's very reason to write is often "the existing file was unusable"
        (`relation_aliases_unreadable`, e.g. a `chmod 000` file) — deriving the
        new file's mode from that SAME file would carry the broken permissions
        onto its own replacement. Measured: with the derive-from-existing
        shape, a POST that successfully rewrote a `chmod 000` alias file with
        valid content still 303'd, but the new file was ALSO mode 0o000, so
        the very next `GET /settings` showed "could not be read" again — a
        "successful" save the user could not then read back.

        `0o644` is NOT equivalent to what `write_text` produced. `write_text`
        PRESERVES an existing file's mode (measured: a `chmod 0o600` file
        stays `0o600` after `write_text` rewrites it) and only applies the
        umask-derived default when CREATING a new file. This writer always
        applies `0o644`, on existing files too — mirroring the exact hazard
        `_write_json_atomic`'s own docstring names for `app.json` ("switching
        to this writer silently tightened `app.json` … on existing files
        too"), except in the opposite direction: a hand-written `0o600` alias
        file (e.g. under `umask 077` — `docs/operations.md`'s
        `policy/relation-aliases.md` row says it is "written by hand or by
        the Settings UI") becomes world-readable `0o644` after one
        Settings-UI save. That widening is accepted, not fixed here
        — the alternative (reading the current umask to replicate
        `write_text`'s create-time behaviour) needs a racy
        `os.umask(0); os.umask(old)` get-and-set in a threaded web process,
        and this route does not have a KB-scoped permissions policy worth
        deriving from the file it is about to replace (the paragraph above).
        The mode is also set via `fchmod` on the temp file's descriptor, which
        ignores the process umask entirely — under `umask 077`, this writer
        still produces `0o644` where a fresh `write_text` would have produced
        `0o600`.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = 0o644
        fd, tmp_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        tmp = Path(tmp_name)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, mode)
            else:
                os.chmod(tmp, mode)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            # `os.replace` MUST be the LAST statement in this `try` (#555 gate
            # rev-8). `save_relation_aliases`'s "Nothing was saved" on any
            # exception out of this function is true only because nothing
            # after a successful rename can still raise — a statement added
            # here, after the rename, would raise into the SAME broad
            # `except Exception` over a file that HAD already changed, and
            # the caller's message would then be false.
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def _override_is_unreadable(path: Path) -> bool:
        """Is the KB's override for this prompt the file that could not be read?

        `get_prompt` reads TWO files — the packaged default and this override —
        so a failed load does not by itself say which one broke, and Reset
        deletes only this one. Measured on a damaged install (packaged default
        at `0o000`) with a healthy override beside it: resetting destroyed the
        user's file and left the page exactly as broken. So the destructive
        control is offered only when the override path is one this process could
        not read. A missing override reads as False — there is nothing there to
        delete, and nothing a reset could fix.

        `is_file()` FIRST, mirroring `get_prompt`, which opens the override only
        behind the same guard, and narrowing this to what `unlink()` can act on:
        without it a directory at that path reads as "unreadable" (measured) and
        the page offers to delete something the loader never opened and
        `unlink()` cannot remove. It sits inside the `try` rather than above it
        because `Path.is_file()` propagates a `PermissionError` from an
        unreadable parent directory (measured), and this whole function runs
        inside the handler that renders the error page — a raise here would take
        that page down instead of the load failure it exists to report.

        This costs an extra `stat` of the override, and a read of it only when
        that `stat` says it is a regular file — a second read when `get_prompt`
        had already reached it, the first when the packaged default is what
        failed. Going back to the disk at all is a stand-in: the exception
        `get_prompt` raises need not say which of the two files it was reading —
        the `PermissionError` from an unreadable one names its path, the
        `UnicodeDecodeError` from a non-UTF-8 one names no file at all
        (measured) — and its text is not a contract to parse.
        """
        try:
            if not path.is_file():
                return False
            path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return False
        except Exception:  # noqa: BLE001 - unreadable by any means is the point
            return True
        return False

    def _prompts_page(
        request: Request,
        *,
        prompt_id: str = "extraction",
        message: str | None = None,
        error: str | None = None,
        prompt_text: str | None = None,
        status_code: int = 200,
    ):
        cfg = app.state.cfg
        if cfg is None:
            return _kb_select(request)
        try:
            prompt = get_prompt(cfg.root, prompt_id)
        except PromptError as exc:
            # Compose the caller's `error` with the load complaint, and honour
            # the caller's status, in the shape of the branch below — with the
            # two departures the paragraphs beneath explain: the
            # `error == load_error` guard, and promoting a caller's 200 to 400
            # rather than passing it through.
            # Replacing them threw away the diagnosis a POST had composed: over
            # a readable-but-invalid override (someone hand-edited the file and
            # dropped a required placeholder), a save that failed to WRITE was
            # reported as "your text must include {qid}" when the user's text
            # did include it, and the caller's status went with it.
            #
            # `error == load_error` and not a bare `error is None`: an unknown
            # `prompt_id` raises the SAME `PromptError` here that the route
            # already caught from its own library call, and composing a string
            # with itself printed `unknown prompt: nope; unknown prompt: nope`.
            #
            # `400 if status_code == 200 else status_code` and not a bare
            # `status_code`: a GET of `/prompts?prompt=nope` passes the default
            # 200 and must still answer 400
            # (`test_a_broken_override_does_not_blank_the_other_prompts`).
            load_error = str(exc)
            return templates.TemplateResponse(
                request,
                "prompts.html",
                {
                    "prompts": list_prompts(),
                    "prompt": None,
                    "selected_prompt": prompt_id,
                    "prompt_text": prompt_text,
                    "message": message,
                    "error": (
                        load_error
                        if error is None or error == load_error
                        else f"{error}; {load_error}"
                    ),
                    "reset_only": False,
                    "override_path": None,
                },
                status_code=400 if status_code == 200 else status_code,
            )
        except Exception as exc:  # noqa: BLE001 - the page that repairs a broken override
            # `get_prompt` reads the override off disk, so a file the user saved
            # here and then made unreadable took this page down with it. Diagnose
            # it in the banner instead: `str(UnicodeDecodeError)` names no file at
            # all (measured — it names a byte offset).
            #
            # NOT the 409 the corrupt-file pages use (`sidecar_unreadable.html`,
            # `config_corrupt.html`, `credentials_corrupt.html`): each of those
            # REPLACES the page the caller asked for with a refusal template and
            # ends there. This one is still a working page: the selector routes
            # to every other prompt, the banner names the prompt that failed,
            # and where the override is itself what cannot be read `reset_only`
            # adds the control that deletes it — `delete_prompt_override` only
            # `unlink()`s, so that repair survives a file this process could not
            # read (measured: 303, the override gone, the page healthy again, in
            # both modes the tests cover). What the page does NOT carry is the
            # broken prompt's SAVED text or an editor seeded from the default:
            # `prompt=None` collapses the editor, and seeding the textarea with
            # the default would invite overwriting a file this process could not
            # read. A refused save POST is the exception, and it seeds
            # nothing: `prompts.html` echoes back the bytes that very request
            # carried, so such a save keeps the user's own typing (#545). Both
            # of the other callers — a GET, and a reset POST — pass no
            # `prompt_text`, so neither is offered an editor at all. So it is a
            # delivered page, not a refused one, and #539's non-regression
            # condition asks for 200 in as many words.
            #
            # `status_code` rather than a literal 200 because
            # `save_prompt_route`/`reset_prompt_route` render this page with 400
            # for a POST they refused; that refusal is still the truth about
            # their request. Their `error` is composed with, not replaced by, the
            # load diagnosis, so such a caller is still told why the save was
            # rejected.
            load_error = f"prompt {prompt_id} could not be loaded: {exc}"
            override_path = prompt_override_path(cfg.root, prompt_id)
            return templates.TemplateResponse(
                request,
                "prompts.html",
                {
                    "prompts": list_prompts(),
                    "prompt": None,
                    "selected_prompt": prompt_id,
                    "prompt_text": prompt_text,
                    "message": message,
                    "error": load_error if error is None else f"{error}; {load_error}",
                    # The only branch that can set this True; the other two
                    # returns of this function pass `False` rather than leaving
                    # it undefined, and those three are every renderer of
                    # `prompts.html` there is (grep). NOT `prompt is None`: the
                    # `except PromptError` branch above renders that way too,
                    # and `unknown prompt: <id>` is one of the things it catches
                    # — no definition, no file, nothing a reset could act on. It
                    # also catches a readable override that fails validation,
                    # where a reset WOULD repair the page and none is offered
                    # (measured); that gap is a follow-up, not this change,
                    # whose subject is an override that cannot be READ.
                    "reset_only": _override_is_unreadable(override_path),
                    "override_path": override_path,
                },
                status_code=status_code,
            )
        return templates.TemplateResponse(
            request,
            "prompts.html",
            {
                "prompts": list_prompts(),
                "prompt": prompt,
                "selected_prompt": prompt.id,
                "prompt_text": prompt.text if prompt_text is None else prompt_text,
                "message": message,
                "error": error,
                "reset_only": False,
                "override_path": None,
            },
            status_code=status_code,
        )

    def _open_root(root: Path) -> None:
        """Point this running app at a KB root, creating it if needed."""
        assert_kb_root_is_safe_to_create(root)
        root = root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        next_cfg = Config.for_root(root)
        # Refuse BEFORE installing: transiently swapping in a corrupt cfg would let
        # a provider call in the gap resolve to the silent cloud default. On a
        # raise the old healthy cfg stays active and the caller renders the halt.
        assert_settings_intact(next_cfg)
        # Deliberately NOT assert_credentials_intact here. That file is
        # machine-wide and byte-identical before and after the switch, so
        # refusing the open prevents no provider call that `get_client`, the job
        # starts and the connection test do not already gate — while making
        # every KB unopenable, including the one the user would switch to in
        # order to select a provider that needs no key. The per-KB reasoning
        # above does not transfer to a machine-wide file.
        next_store = Store(next_cfg.db_path)
        next_store.init_schema()

        # Adopt an existing policy file, then scaffold a default one *only* for a
        # KB that never recorded a policy. A KB whose recorded policy file is gone
        # is opened as-is: re-writing the default here would hide the loss, so the
        # KB stays open and /report surfaces the error instead.
        ensure_policy_marker(next_store, root)
        if resolve_policy(next_store).status is PolicyStatus.UNRECORDED_DEFAULT:
            write_default_policy(next_store, root, origin="scaffold")

        old_store = app.state.store
        if old_store is not None:
            old_store.close()
        app.state.cfg = next_cfg
        app.state.store = next_store

    def _kb_select(request: Request, *, error: str | None = None, status_code: int = 200):
        return templates.TemplateResponse(
            request,
            "kb_select.html",
            {"error": error},
            status_code=status_code,
        )

    def _fact_view(fact):
        if fact is None:
            return None
        store = _active_store()
        view = dict(fact)
        # A raise here (corrupt/unreadable sidecar) must propagate to the shared
        # DuckDBFactTermStoreError handler, not be swallowed: silently treating it
        # as `terms is None` would render a structural fact as a plain string,
        # making genuine corruption indistinguishable from a real string fact.
        terms = store.get_fact_terms(fact["id"])
        if terms is None:
            for field in ("subject", "relation", "object"):
                view[f"{field}_display"] = fact[field]
                view[f"{field}_edit"] = fact[field]
                view[f"{field}_kind"] = "string"
            return view
        for field, term in zip(("subject", "relation", "object"), terms, strict=True):
            view[f"{field}_display"] = render_term(term)
            view[f"{field}_edit"] = term.value if isinstance(term, StringLit) else render_term(term)
            view[f"{field}_kind"] = term_input_kind(term)
        return view

    def _fact_row_context(fact, recommendations=None, *, policy_error: str | None):
        """Row context. `policy_error` withholds every trust-derived signal (#570).

        `policy_error` is REQUIRED and keyword-only on purpose. The recurring
        defect here is a caller that reaches a policy-file read without guarding
        it (#570 trap 1): a defaulted parameter turns the next such caller into a
        silent 500 in production, a required one turns it into a `TypeError` at
        the call site.

        When it is set, `trust` and `recommendation` are `None` rather than
        computed anyway, and the two files reach that conclusion by different
        routes. `store_relation_aliases` returns
        `merge_default_relation_aliases(user_aliases)`, so the delta between the
        user's file and the defaults is exactly their custom entries — i.e. the
        only reason the file exists. A badge computed without them is a number
        about a KB nobody has, rendered in the same badges a healthy KB uses.
        `store_typed_relations` has no defaults to fall back to: it degrades to
        `{}`, which is exactly what a KB with no typed declarations returns, so
        a badge computed without that file is INDISTINGUISHABLE from a healthy
        one and nothing in the render could give it away (#585). Either way the
        answer is to withhold, not to approximate.

        `None` is also what lets `fact_row.html` say "not computed" instead of
        borrowing the `trust unavailable` verdict below it, which means
        something else and something measured: this fact has no trust summary.
        """
        view = _fact_view(fact)
        if policy_error is None:
            trust = fact_trust_summary(_active_store(), int(fact["id"])) if fact else None
            if fact and recommendations is None:
                recommendations = accept_recommendations(_active_store())
            recommendation = recommendations.get(int(fact["id"])) if fact else None
        else:
            trust = None
            recommendation = None
        return {
            "f": view,
            "trust": trust,
            "recommendation": recommendation,
            "actionable": bool(fact and is_actionable_fact_status(fact["status"])),
            "policy_error": policy_error,
        }

    def _actionable_fact_or_error(fact_id: int):
        fact = _active_store().get_fact(fact_id)
        if fact is None:
            raise HTTPException(status_code=404, detail="fact not found")
        if not is_actionable_fact_status(fact["status"]):
            raise HTTPException(status_code=400, detail="fact is not actionable")
        return fact

    def _maybe_apply_auto_accept(
        exclude_fact_ids: tuple[int, ...] = (), *, policy_error: str | None
    ) -> list:
        """Run the auto-accept pass, unless a trust-policy file cannot be read.

        The only guard in this change that stops a WRITE (#570).
        `apply_auto_accept_recommendations` promotes facts to `accepted` and
        retracts lapsed ones, and it decides which by reading BOTH policy files
        (`acceptance.py::_engine` reads the alias file and the typed file on
        consecutive lines). A pass run while either is unreadable would rewrite
        the KB's review state under rules the user did not configure — the
        packaged alias defaults, or no typed declarations at all. A badge
        computed on the wrong rules is re-rendered on the next request; a status
        transition is committed and audited.

        `policy_error` is required and keyword-only for the same reason as on
        `_fact_row_context`.
        """
        if policy_error is not None:
            return []
        if _active_cfg().auto_accept_recommendations:
            return apply_auto_accept_recommendations(
                _active_store(), exclude_fact_ids=exclude_fact_ids
            )
        return []

    def _row(request: Request, fact, *, policy_error: str | None):
        # Starlette's current API is TemplateResponse(request, name, context).
        return templates.TemplateResponse(
            request,
            "partials/fact_row.html",
            _fact_row_context(fact, policy_error=policy_error),
        )

    def _row_after_decision(
        request: Request,
        fact,
        acted_fact_id: int | None,
        *,
        rule_may_act: bool = True,
        decided: bool = True,
    ):
        """Render the acted row, running auto-accept for the corroboration it
        may have unblocked.

        A human decision changes the corroboration landscape, so re-run the
        recommender here just as the extraction worker does. When it promotes
        *other* facts, a single-row HTMX swap can't reveal them, so ask the
        client for a full refresh; when nothing (or only the acted fact) moved,
        the row swap is enough. The acted row is re-read afterwards so it
        reflects an auto-accept that landed on the fact itself.

        `decided=False` means the POST changed nothing — a replayed accept on an
        already-confirmed fact, a toggle a reject beat to the row. The follow-on
        pass is owed to a *transition*, not to a request arriving: with no new
        human decision there is no newly unblocked corroboration, and running the
        rule anyway would promote siblings and stamp `auto_accepted` audit events
        off a click the user may have made hours ago (or never made — HTMX and
        browsers both retry). Such a request only re-renders the row.

        `rule_may_act=False` bars the rule from the acted fact while still
        letting it promote everything else — for the one decision that parks a
        fact back in the tier auto-accept harvests from (see `toggle`).
        """
        # One policy-file check per decision POST, threaded into both the
        # auto-accept pass and the row render, rather than one per consumer
        # (#570).
        policy_error = _trust_policy_failure(_active_store())
        excluded = () if rule_may_act or acted_fact_id is None else (acted_fact_id,)
        applied = (
            _maybe_apply_auto_accept(excluded, policy_error=policy_error) if decided else []
        )
        if acted_fact_id is not None:
            refreshed = _active_store().get_fact(acted_fact_id)
            if refreshed is not None:
                fact = refreshed
        response = _row(request, fact, policy_error=policy_error)
        if any(rec.fact_id != acted_fact_id for rec in applied):
            response.headers["HX-Refresh"] = "true"
        return response

    def _mark_saved_fact_decision(request: Request, action: str, decision) -> None:
        """Carry one committed review-log record to a later sidecar halt."""
        if not decision.changed or decision.fact is None:
            return
        fact_id = int(decision.fact["id"])
        log = _active_store().fact_log(fact_id)
        if log and log[-1]["action"] == _FACT_DECISION_LOG_ACTIONS[action]:
            request.state.saved_fact_decision = (fact_id, action, int(log[-1]["id"]))

    def _fact_edit_context(fact, *, error: str | None = None):
        kinds = {"subject": "string", "relation": "string", "object": "string"}
        if fact is not None:
            store = _active_store()
            terms = store.get_fact_terms(fact["id"])
            if terms is not None:
                kinds = {
                    "subject": term_input_kind(terms[0]),
                    "relation": term_input_kind(terms[1]),
                    "object": term_input_kind(terms[2]),
                }
        return {"f": _fact_view(fact), "kinds": kinds, "error": error}

    def _fact_input(value: str, kind: str):
        if kind == "string":
            return nfc(value)
        if kind == "term":
            return nfc_term(structural_term(value))
        raise ValueError(f"unknown fact input kind: {kind}")

    def _review_filters() -> list[tuple[str, str]]:
        return [
            ("needs-human-decision", "Needs decision"),
            ("unsupported", "Unsupported"),
            ("single-source", "Single source"),
            ("corroborated", "Corroborated"),
            ("conflicted", "Conflicted"),
        ]

    def _active_review_filter(active_filter: str) -> str:
        labels = {key for key, _ in _review_filters()}
        return active_filter if active_filter in labels else "needs-human-decision"

    def _review_url(
        *,
        active_filter: str,
        sort: str,
        page_size: int,
        page: int = 1,
    ) -> str:
        return "/review?" + urlencode(
            {
                "filter": active_filter,
                "sort": sort,
                "page_size": page_size,
                "page": page,
            }
        )

    def _review_filter_links(active_filter: str, sort: str, page_size: int):
        return [
            {
                "key": key,
                "label": label,
                "href": _review_url(
                    active_filter=key,
                    sort=sort,
                    page_size=page_size,
                    page=1,
                ),
                "active": active_filter == key,
            }
            for key, label in _review_filters()
        ]

    def _review_pages(active_filter: str, sort: str, page_size: int, page: int, page_count: int):
        candidates = {1, page_count}
        for nearby in range(page - 2, page + 3):
            if 1 <= nearby <= page_count:
                candidates.add(nearby)
        pages = []
        last = 0
        for number in sorted(candidates):
            if last and number > last + 1:
                pages.append({"ellipsis": True})
            pages.append(
                {
                    "number": number,
                    "active": number == page,
                    "href": _review_url(
                        active_filter=active_filter,
                        sort=sort,
                        page_size=page_size,
                        page=number,
                    ),
                }
            )
            last = number
        return pages

    def _review_pager(active_filter: str, page_data):
        page_count = page_data.page_count
        page = page_data.page
        return {
            "total": page_data.total,
            "start": page_data.start,
            "end": page_data.end,
            "page": page,
            "page_size": page_data.page_size,
            "page_count": page_count,
            "sort": page_data.sort,
            "page_sizes": REVIEW_PAGE_SIZES,
            "sort_options": [
                ("newest", "Newest"),
                ("oldest", "Oldest"),
                ("updated", "Recently updated"),
                ("confidence", "Confidence"),
                ("source", "Source"),
            ],
            "prev_href": (
                _review_url(
                    active_filter=active_filter,
                    sort=page_data.sort,
                    page_size=page_data.page_size,
                    page=page - 1,
                )
                if page > 1
                else None
            ),
            "next_href": (
                _review_url(
                    active_filter=active_filter,
                    sort=page_data.sort,
                    page_size=page_data.page_size,
                    page=page + 1,
                )
                if page < page_count
                else None
            ),
            "pages": _review_pages(
                active_filter, page_data.sort, page_data.page_size, page, page_count
            ),
        }

    def _review_page(store: Store, active_filter: str, page: str, page_size: str, sort: str):
        if active_filter == "needs-human-decision":
            return store.review_queue_page(page=page, page_size=page_size, sort=sort)
        label = active_filter.replace("-", "_")
        matching_ids = [
            fact_id
            for fact_id in store.review_queue_ids(sort=sort)
            if (summary := fact_trust_summary(store, fact_id)) is not None
            and label in summary.trust_labels
        ]
        page_data = ReviewQueuePage.from_rows(
            rows=[],
            total=len(matching_ids),
            page=page,
            page_size=page_size,
            sort=sort,
        )
        start = (page_data.page - 1) * page_data.page_size
        rows = store.facts_by_ids(matching_ids[start : start + page_data.page_size])
        return ReviewQueuePage.from_rows(
            rows=rows,
            total=len(matching_ids),
            page=page_data.page,
            page_size=page_data.page_size,
            sort=page_data.sort,
        )

    def _source_inspector_rows(
        store: Store, *, policy_error: str | None
    ) -> list[dict[str, object]]:
        facts = store.facts()
        # Compute the rollup only when both trust-policy files are usable — a
        # `policy_error` means `_source_trust_rollup` would 500 the same way this
        # route used to (#555 for the alias file, #585 for the typed one, which
        # it reads on the next line).
        # One `store.facts()` scan either way: passing `policy_error` in and
        # branching here, rather than a caller-side `None if policy_error else
        # _source_trust_rollup(store, store.facts())`, avoids a second full scan on
        # every healthy request.
        trust_rollup = None if policy_error else _source_trust_rollup(store, facts)
        rows = []
        for source in store.sources_with_counts():
            row = dict(source)
            source_id = int(source["id"])
            if trust_rollup is None:
                # Leave the count keys unset rather than zeroing them: a template
                # that forgot this branch renders a blank badge (Jinja's default
                # `Undefined` renders '', it does not raise), which is still
                # honest — a false "0 unsupported" on a KB that was never checked
                # is not.
                row["trust_unavailable"] = True
            else:
                counts = trust_rollup.get(
                    source_id,
                    {"unsupported": 0, "conflicted": 0, "corroborated": 0},
                )
                row["unsupported_count"] = counts["unsupported"]
                row["conflicted_count"] = counts["conflicted"]
                row["corroborated_count"] = counts["corroborated"]
            row["evidence_snippets"] = store.source_evidence_snippets(source_id)
            row["artifacts"] = [dict(artifact) for artifact in store.source_artifacts(source_id)]
            row["failed_chunk_details"] = []
            row["pending_chunks"] = 0
            row["running_chunks"] = 0
            if source["job_id"]:
                chunks = store.source_chunks(int(source["job_id"]))
                row["failed_chunk_details"] = [
                    dict(chunk) for chunk in chunks if chunk["status"] == "failed"
                ]
                # `pending` and `running` are counted apart. Summed, the page read
                # "49 pending" for a job holding 48 untouched chunks and one it had
                # already claimed and sent to the LLM (#475) — which says none of
                # this has been started. They are different states to anyone who
                # has to act on them, so they are different numbers here.
                row["pending_chunks"] = sum(
                    1 for chunk in chunks if chunk["status"] == "pending"
                )
                row["running_chunks"] = sum(
                    1 for chunk in chunks if chunk["status"] == "running"
                )
            rows.append(row)
        return rows

    def _source_trust_rollup(store: Store, facts) -> dict[int, dict[str, int]]:
        aliases = store_relation_aliases(store)
        typed = store_typed_relations(store)
        support_sources: dict[tuple[str, str, tuple[str, object]], set[str]] = {}
        for fact in facts:
            if not is_engine_input(fact["status"]):
                continue
            source_path = str(fact["source_path"] or "").strip()
            if not source_path:
                continue
            relation = canonical_relation(str(fact["relation"]), aliases)
            support_sources.setdefault(
                (
                    str(fact["subject"]),
                    relation,
                    _source_object_key(relation, str(fact["object"]), typed),
                ),
                set(),
            ).add(source_path)

        conflict_keys = {
            (conflict.subject, conflict.relation)
            for conflict in store_single_valued_conflicts(store)
        }
        counts: dict[int, dict[str, int]] = {}
        for fact in facts:
            if fact["source_id"] is None:
                continue
            source_id = int(fact["source_id"])
            bucket = counts.setdefault(
                source_id,
                {"unsupported": 0, "conflicted": 0, "corroborated": 0},
            )
            relation = canonical_relation(str(fact["relation"]), aliases)
            support_count = len(
                support_sources.get(
                    (
                        str(fact["subject"]),
                        relation,
                        _source_object_key(relation, str(fact["object"]), typed),
                    ),
                    set(),
                )
            )
            if support_count == 0:
                bucket["unsupported"] += 1
            elif support_count > 1:
                bucket["corroborated"] += 1
            if (str(fact["subject"]), relation) in conflict_keys:
                bucket["conflicted"] += 1
        return counts

    def _source_object_key(relation: str, obj: str, typed) -> tuple[str, object]:
        spec = typed.get(relation) or typed.get(unicodedata.normalize("NFC", relation))
        if spec is not None:
            scalar = normalize_typed_value(spec.type, obj, spec.units)
            if scalar is not None:
                return ("scalar", scalar)
        return ("raw", obj)

    def _dashboard_queues(
        store: Store, *, policy_error: str | None
    ) -> list[dict[str, object]]:
        # `jobs` and `recent_lifecycle` are policy-file-independent (measured,
        # #555 M5) and keep their real counts either way. The other four rows
        # depend on `fact_trust_summary`, `trust_workbench`, or
        # `store_corroboration` — every one of which reads the alias file, and
        # the first two the typed file as well — so when either file cannot be
        # applied they show `None`
        # (rendered as "not computed" by the template) rather than a count computed
        # under rules the user did not configure, or a false `0`.
        jobs = store.source_extraction_jobs()
        recent_lifecycle = store.count_facts_with_events(("amended", "reanalyzed"))
        if policy_error is None:
            review_summaries = [
                fact_trust_summary(store, int(fact["id"])) for fact in store.review_queue()
            ]
            review_summaries = [summary for summary in review_summaries if summary is not None]
            workbench = trust_workbench(store)
            corroboration = store_corroboration(store)
            unsupported_count = sum(
                1 for summary in review_summaries if "unsupported" in summary.trust_labels
            )
            corroborated_review_count = sum(
                1 for summary in review_summaries if "corroborated" in summary.trust_labels
            )
            conflicts_count = len(workbench.conflicts)
            engine_facts_count = len(corroboration)
        else:
            unsupported_count = None
            corroborated_review_count = None
            conflicts_count = None
            engine_facts_count = None
        return [
            {
                "label": "Unsupported review items",
                "count": unsupported_count,
                "href": "/review?filter=unsupported",
                "detail": "candidate facts without deterministic source support",
            },
            {
                "label": "Corroborated review targets",
                "count": corroborated_review_count,
                "href": "/review?filter=corroborated",
                "detail": "review items backed by repeated source support",
            },
            {
                "label": "Single-valued conflicts",
                "count": conflicts_count,
                "href": "/workbench",
                "detail": "accepted/confirmed values competing under functional rules",
            },
            {
                "label": "Failed source analyses",
                "count": sum(1 for job in jobs if job["status"] == "failed"),
                "href": "/sources",
                "detail": "sources with failed extraction chunks ready for retry",
            },
            {
                "label": "Recent lifecycle changes",
                "count": int(recent_lifecycle),
                "href": "/review",
                "detail": "facts amended or reanalyzed after extraction",
            },
            {
                "label": "Source-backed engine facts",
                "count": engine_facts_count,
                "href": "/workbench",
                "detail": "accepted/confirmed facts with source support",
            },
        ]

    def _dashboard(request: Request, *, error: str | None = None, status_code: int = 200):
        from verinote.engine import coverage

        if app.state.store is None:
            return _kb_select(request, error=error, status_code=status_code)
        store = _active_store()
        cfg = _active_cfg()
        policy_error = _trust_policy_failure(store)
        counts = store.status_counts()
        # `counts`, `total`, `engine_input`, `sources`, `coverage` are all
        # policy-file-independent (measured, #555 M5) and keep their real values
        # even when a policy file is broken. `corroboration` reads the alias
        # file and `single_valued_conflicts` reads both; `None` (not `[]`) so the
        # template can tell "not computed" apart from "computed, and empty" —
        # `[]` would render the same "No source-backed …" prose a healthy KB with
        # nothing to show gets, which is a false statement about a KB that was
        # never analysed.
        if policy_error is None:
            corroboration = store_corroboration(store)
            single_valued_conflicts = store_single_valued_conflicts(store)
        else:
            corroboration = None
            single_valued_conflicts = None
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "counts": counts,
                "total": sum(counts.values()),
                # Derived here, not summed in the template: the dashboard's
                # "engine input" card must answer the same question as coverage
                # and the Sources badge, from the same constant.
                "engine_input": sum(counts.get(status, 0) for status in engine_statuses()),
                "all_fact_statuses": fact_status_order(),
                "sources": store.sources(),
                "coverage": coverage(store, root=cfg.root),
                "corroboration": corroboration,
                "single_valued_conflicts": single_valued_conflicts,
                "queues": _dashboard_queues(store, policy_error=policy_error),
                "policy_error": policy_error,
                "provider": app.state.cfg.provider,
                "provider_label": PROVIDER_LABELS.get(
                    app.state.cfg.provider, app.state.cfg.provider
                ),
                "model": app.state.cfg.model,
                "root": app.state.cfg.root,
                "error": error,
            },
            status_code=status_code,
        )

    def _sources(request: Request, *, error: str | None = None, status_code: int = 200):
        if app.state.store is None:
            return _kb_select(request, error=error, status_code=status_code)
        store = _active_store()
        policy_error = _trust_policy_failure(store)
        jobs = store.source_extraction_jobs()
        latest_job_ids = latest_source_job_ids(jobs)
        # A superseded `pending` row is dead work, and counting it here is what
        # leaves the page polling every 2s forever and claiming an analysis is in
        # flight when nothing is processing it.
        has_running_jobs = any(is_live_extraction_job(job, latest_job_ids) for job in jobs)
        return templates.TemplateResponse(
            request,
            "sources.html",
            {
                "sources": _source_inspector_rows(store, policy_error=policy_error),
                "suffixes": ", ".join(sorted(supported_suffixes())),
                "accept": ",".join(sorted(supported_suffixes())),
                "error": error,
                "policy_error": policy_error,
                "jobs": jobs,
                "has_running_jobs": has_running_jobs,
                "chunk_chars": app.state.cfg.extraction_chunk_chars,
                "max_facts_per_chunk": app.state.cfg.extraction_max_facts_per_chunk,
            },
            status_code=status_code,
        )

    def _start_source_extraction(
        job_id: int,
        cfg: Config,
        *,
        retry: bool = False,
        retry_max_attempts: int | None = None,
    ) -> None:
        # SYNCHRONOUS and OUTSIDE run(): raise on the triggering request itself so a
        # corrupt config returns an immediate 409 instead of silently queuing a
        # background job doomed to reach the cloud default. The worker's own
        # get_client(cfg) below stays as a narrow backstop for the race window
        # between this check and the thread actually running (#269).
        assert_settings_intact(cfg)
        assert_credentials_intact(cfg)

        def _fail_job_unless_done(message: str) -> None:
            """Record a worker-level failure — iff this call still owns the job.

            THE JOB-LEVEL FLOOR FOR THE WEB WORKER (#525), and the third of three
            siblings that all ask one question: `_release_claimed_chunk`
            (`pipeline/extract.py`) asks it of a chunk, `cmd_sync`'s job-level
            clause (`cli.py`, #488) asks it of a CLI job. As in theirs, the status
            re-read is not a second decision about whether this pass failed. It
            declines to write a job this call no longer owns. `mark_chunk_done`
            writes the job `done` when the last chunk lands and
            `finish_extraction_job` runs AFTER that, so a raise there escapes with
            the job already `done`, every chunk complete and the candidates
            committed — measured, and without this guard recorded as
            `failed: analysis failed: ...` with an `extraction_job_failed` event
            beside it.

            IT ONLY REFUSES `done`, AND THAT IS NOT THE CLI'S PREDICATE. `cmd_sync`
            writes only a `running` job, and copying that here would not tighten
            this clause but silently gut it: this worker's `try` also spans
            `get_client(cfg)` and the `_extraction_schema_hint(cfg)` argument
            expression, BOTH evaluated before `process_extraction_job` claims the
            job. Measured — the row read from inside the raising callable is
            `pending` — and measured again through the suite: substituting
            `cmd_sync`'s `running` predicate on these two clauses reddens
            `test_worker_still_fails_the_job_on_an_ordinary_error`, both
            parametrizations of
            `test_a_broken_extraction_limit_hint_is_extraction_failed_not_analysis_failed`,
            and `test_worker_writes_nothing_when_the_job_row_is_gone` — whose row is
            gone, so no status matches and the write is declined instead of being
            the no-op the bullet below describes. Measured: those four are the whole
            red set in the suite. The two `done` tests reach these clauses too and
            stay green — a `done` job is declined under either predicate — so being
            not `running` is not by itself what reddens a test; requiring the write
            is. The point is that a pre-claim failure would stop being recorded at
            all. The CLI
            clause's `try` contains no pre-claim code of its own — `client` and
            `schema_hint` are both computed above it (`cli.py`), and none of the
            call's argument expressions is itself a call. The predicates differ
            because the scopes do.

            REFUSING ONLY `done` ALSO KEEPS THE CLAUSE-ORDER TESTS HONEST.
            `test_worker_halt_does_not_mark_the_job_failed` and
            `test_worker_busy_does_not_mark_the_job_failed` both leave the job
            `pending`, so this guard still WRITES on their paths and the clauses
            above remain the only thing between a halted or foreign-owned job and a
            `failed` row. Under a `running` predicate both would pass with those
            clauses deleted, the way `cli.py` records its own pair going
            non-distinguishing.

            AND `running` MUST STILL BE WRITTEN, which is the other end of the same
            decision. A chunk failing for a non-`LLMError` reason leaves the job
            `running` — `_refresh_extraction_job` deliberately keeps an owned job
            `running` across the release (#337) — and that is the ordinary failure
            these clauses exist to report. Widening the refusal to
            `{"done", "running"}` is caught by exactly ONE test, measured:
            `test_worker_still_fails_a_claimed_job_whose_chunk_crashed` goes red
            while no other `test_worker_*` test does. Do not widen this without
            reading that test.

            WHAT IT DOES NOT COVER — each bullet says which status it leaves the job
            in and how close to reachable it is; they do not share an answer:
            - A FUTURE REWINDING PATH. The two paths that rewind a job to `pending`
              (`_halt_extraction_job`, `_back_off_from_locked_sidecar`) re-raise
              types the `PolicyMissingError` and `DuckDBFactTermStoreLockedError`
              clauses take ABOVE these two, so no rewind reaches here. A new one
              that did would arrive `pending` and be buried, since `done` is the
              only status this refuses. A new rewinding path in this worker
              therefore needs its own clause above `except LLMError`; it cannot
              lean on this guard.
            - A PEER THAT REWINDS IN THE WINDOW. The re-read and the write are two
              statements on an autocommit connection, and `fail_extraction_job`
              updates `WHERE id = ?` with no status predicate, so this holds against
              a job that was already `done` when the read ran, not against one
              rewound between the two. `_resume_source_extraction_jobs` rolls
              `running` jobs back to `pending` at `create_app()` time and cannot
              tell a crashed zombie from a live owner (#242), so for a web app a
              second boot against the same KB is the concrete shape of that peer.
              Closing it means moving the predicate into the SQL, as the extraction
              path's ownership handshakes already do — a store change, not made here.
            - AN ALREADY-`failed` JOB, whose detailed per-chunk message this can
              still overwrite. `finish_extraction_job` runs on an autocommit
              connection, so its `_refresh_extraction_job(final=True)` UPDATE
              commits BEFORE its `extraction_job_completed` event is appended. A
              raise at that last step therefore leaves whatever `final=True`
              computed — `failed` when a chunk failed — and this guard permits the
              write over it. MEASURED, driving the real worker on a 2-chunk job
              whose first chunk raised `LLMError` and whose
              `extraction_job_completed` append was forced to raise: the job comes
              to rest `failed` with "analysis failed: post-final event append
              failed" and a second `extraction_job_failed` event beside the
              `chunk_failed` one, where a refusal set widened to
              `("done", "failed")` keeps "Analysis failed: 1 chunk(s) failed, 1/2
              complete: chunk one llm failure" and appends no second failure event.
              Widening is still not the fix: the retry button's worker evaluates
              `get_client(cfg)` and `_extraction_schema_hint(cfg)` while the job is
              STILL the previous run's `failed` — the status only moves inside
              `process_extraction_job`, at `claim_extraction_job_for_retry` — so
              refusing `failed` would leave a retry that dies pre-claim showing the
              old message and recording nothing on the job row. Nothing pins either
              behaviour; #552 tracks it.
            - A `canceled` JOB. This guard refuses `done` BY NAME, so every other
              status the CHECK on `extraction_jobs.status` admits (`schema.sql`) is
              written over, and `canceled` is one of them. That contradicts what
              the store itself does with such a job:
              `Store.rollback_extraction_job` treats `canceled` as sacred and
              returns above every write it would otherwise make rather than
              touch it, while this guard would put `failed` on that row with an
              `extraction_job_failed` event beside it. Unreachable today — no
              production code under `verinote/` writes `'canceled'` at all — which
              makes it a third latent site for #526. That issue's two items are
              `mark_extraction_job_running` and `_refresh_extraction_job`; neither
              names this one, so a cancel feature that fixed #526 as written would
              still ship this guard writing over the cancellation.
            - A JOB WHOSE SOURCE WAS DELETED, read back as `None`. The write goes
              ahead and is a no-op: `fail_extraction_job` matches no row and appends
              no event. Branching on it would be dead code, and no longer UNtested
              dead code: `test_worker_writes_nothing_when_the_job_row_is_gone` asserts
              the call is made, so an `if job_now is None: return` added here reddens
              it. Measured both ways — that branch is green on the tree before that
              test existed and red with it.

            DECLINING IS NOT DROPPING. The error that brought us here is real — on
            the #525 path it is a genuine sqlite/WAL-class failure — and refusing the
            job row must not also refuse the record. Nothing else records it: the
            job keeps a `done` row, and because `finish_extraction_job` raised
            part-way there is no `extraction_job_completed` event and no run summary
            either. So the decline logs, exactly as the four write-nothing clauses
            above it do
            (`PolicyMissingError`, `ExtractionJobBusyError`, the ConfigCorrupt pair,
            `DuckDBFactTermStoreLockedError`). The CLI counterpart does not go quiet
            on its own decline either — it re-raises, and `main` surfaces it — but a
            daemon worker thread has nowhere to re-raise TO, so the log is the whole
            of the record.

            AND THE READ CARRIES A WRITE'S WORTH OF RISK. Both clauses already
            opened a fresh `Store` and ran `init_schema()` inside the handler before
            this helper existed, so a store error replacing the escaping exception
            (the original left on `__context__`) is not new; this adds one SELECT to
            that surface. It is deliberately NOT wrapped in a `try`: a raise here
            reaches the thread excepthook, and the job state it leaves is the same
            one a declining guard leaves — nothing written. Silence would be worse.
            """
            with Store(cfg.db_path) as worker_store:
                worker_store.init_schema()
                job_now = worker_store.get_extraction_job(job_id)
                if job_now is not None and job_now["status"] == "done":
                    # The status is interpolated rather than spelled "already done":
                    # it is the second place the predicate would otherwise be
                    # encoded in prose, and a widened refusal set would silently
                    # make a hardcoded reason lie. `exc_info` is not decoration —
                    # the row would gain only the exception's TYPE when its message
                    # is blank (`_error_cause`, #551), never the traceback, so for a
                    # bare `ValueError()` the formatted text would read "analysis
                    # failed: ValueError" and the traceback here is still the only
                    # surviving record of anything more specific than that.
                    logger.warning(
                        "extraction job %s is %s; not recording on the job row: %s",
                        job_id,
                        job_now["status"],
                        message,
                        exc_info=True,
                    )
                    return
                worker_store.fail_extraction_job(job_id, message)

        def run() -> None:
            try:
                with Store(cfg.db_path) as worker_store:
                    worker_store.init_schema()
                    client = get_client(cfg)
                    result = process_extraction_job(
                        worker_store,
                        client,
                        job_id=job_id,
                        schema_hint=_extraction_schema_hint(cfg),
                        retry=retry,
                        retry_max_attempts=retry_max_attempts,
                    )
                    # A clean run judges staleness: a confirmed/accepted citation
                    # whose source text changed under it returns to review (#329).
                    # The sweep is a SIBLING of `process_extraction_job`, not folded
                    # inside it, for separation of concerns: extraction stays a pure
                    # primitive, while the sweep's return value (the demoted fact
                    # ids) is needed HERE to thread into `exclude_fact_ids` below.
                    # Sibling placement does not by itself keep this call out of the
                    # outer clauses — it still sits in the same try. Since #525 it no
                    # longer has to: `_fail_job_unless_done` re-reads the status and
                    # declines to write an already-`done` job, so a sweep error can
                    # no longer flip a completed run to `failed` whether the guard
                    # below catches it or not. MEASURED: with that guard removed the
                    # worker/sweep/auto-accept tests all still pass, so no test
                    # distinguishes deleting it. What it still buys is narrower and
                    # currently untested — auto-accept below RUNS after a failed
                    # sweep instead of being skipped with it — and untested because
                    # the one fixture that raises here builds a Config leaving
                    # `auto_accept_recommendations` at its `False` default. Kept as
                    # defence in depth and as that scope narrowing, not as the thing
                    # standing between a `done` job and a `failed` row.
                    # `assert_writable` runs first (and OUTSIDE that
                    # guard) so a policy that vanished post-completion routes to the
                    # PolicyMissingError handler instead of demoting facts against a
                    # halted KB (#194) — the store layer trusts its caller for this,
                    # exactly as `process_extraction_job` and auto-accept do.
                    demoted_ids: tuple[int, ...] = ()
                    if result.failed_chunks == 0:
                        assert_writable(worker_store)
                        try:
                            demoted_ids = tuple(
                                int(row["id"])
                                for row in worker_store.surface_stale_engine_facts(job_id)
                            )
                        except Exception:  # noqa: BLE001 - a sweep error must not fail a done job
                            # The sweep does no LLM/network I/O, so a raise here is a
                            # rare sqlite/WAL-lock-class error. Contain it: the
                            # extraction genuinely succeeded, so take no demotions
                            # this pass and carry on to auto-accept. Since #525 the
                            # job staying `done` is no longer this clause's doing —
                            # the outer handler re-reads and declines — so what is
                            # contained here is the SKIP, not the burial.
                            logger.warning(
                                "stale-citation sweep failed for job %s; leaving it done",
                                job_id,
                                exc_info=True,
                            )
                    if cfg.auto_accept_recommendations:
                        # Exclude just-demoted facts so THIS request's auto-accept
                        # pass can't demote-then-immediately-re-promote them. Part
                        # C's `stale` flag is what blocks re-promotion on later
                        # syncs; this only ever covered the same-pass case.
                        try:
                            apply_auto_accept_recommendations(
                                worker_store, exclude_fact_ids=demoted_ids
                            )
                        except PolicyMissingError:
                            # ORDER IS LOAD-BEARING — this must stay ABOVE `except
                            # Exception`. Auto-accept runs `assert_writable` as its
                            # own first act (acceptance.py); a policy lost
                            # post-completion is a #194 halt that must reach the
                            # outer PolicyMissingError handler (which writes
                            # NOTHING), never be contained here as if it were an
                            # ordinary failure.
                            raise
                        except Exception:  # noqa: BLE001 - an auto-accept error must not fail a done job
                            # Auto-accept does no LLM/network I/O, so a raise here is
                            # a rare sqlite/WAL-lock-class error. The extraction
                            # genuinely succeeded and its facts are already committed
                            # (#340; sibling of the #329 sweep guard directly above).
                            # Since #525 this clause is no longer what leaves the job
                            # `done` either — and being the LAST statement in the try,
                            # it buys even less than the sweep guard does: MEASURED,
                            # removing it leaves the same worker/sweep/auto-accept
                            # tests green, because the error would reach the outer
                            # handler and be declined there. What survives is the
                            # targeted log line below — the `raise` above it is not
                            # something THIS clause buys: it exists only to keep
                            # this clause off the halt path, and deleting the whole
                            # inner try/except lets a #194 halt reach the outer
                            # handler unaided.
                            logger.warning(
                                "auto-accept failed for job %s; leaving it done",
                                job_id,
                                exc_info=True,
                            )
            except PolicyMissingError as e:
                # ORDER IS LOAD-BEARING — this must stay ABOVE `except Exception`.
                # The worker runs outside the request middleware, so a halt surfaces
                # here as an ordinary exception; the generic handler below would
                # "report" it by calling `fail_extraction_job` — a WRITE to the very
                # KB the halt exists to protect, and one that buries the job in a
                # `failed` state nothing resumes. So this handler writes NOTHING and
                # only logs. (#194)
                #
                # It catches halts from three places, and they leave the job in
                # different states: `process_extraction_job` has already rolled a
                # mid-job halt back to `pending`, while the pre-sweep
                # `assert_writable` and `apply_auto_accept_recommendations` both halt
                # *after* the job finished `done`, with no rollback at all. The
                # message must therefore not assert a rollback
                # — a log line claiming one for a `done` job would be the same class
                # of falsehood this change removes. Whoever rewinds, rewinds; this
                # handler reports.
                logger.warning("extraction job %s halted (KB policy missing): %s", job_id, e)
            except ExtractionJobBusyError:
                # Another worker owns this job (a concurrent sync, a second UI
                # worker, or another startup resume). It may have a chunk in
                # flight; ANY write here — including `fail_extraction_job` — would
                # corrupt a job we do not own. Log and leave it entirely. (#240)
                logger.info(
                    "extraction job %s already owned by another worker; not started here",
                    job_id,
                )
            except (ConfigCorruptError, CredentialsCorruptError) as exc:
                # ORDER IS LOAD-BEARING — above `except Exception`. Defense-in-depth,
                # NOT a currently-reachable path: the worker's get_client(cfg) reads
                # the SAME frozen cfg that already passed the synchronous hoist check
                # above, and `cfg.settings_error` is a one-time snapshot taken in
                # `Config.for_root`, so today the thread can never independently
                # observe a fresher corruption than the hoist already cleared. This
                # clause exists so that if that ever changes — a future get_client
                # that re-reads disk, or a caller that passes a *different* cfg into
                # the thread — a corrupt config (a host/environment condition, not
                # content-attributable) still writes NOTHING here (mirror
                # `except PolicyMissingError`) instead of falling through to
                # `except Exception`, which would call `fail_extraction_job` —
                # burying the job in `failed` with a misleading "analysis failed" and
                # consuming this session's MAX_CHUNK_ATTEMPTS retry budget for a cause
                # unrelated to the source content (#269).
                logger.warning(
                    "extraction job %s halted (%s): %s", job_id, type(exc).__name__, exc
                )
            except DuckDBFactTermStoreLockedError as exc:
                # ORDER IS LOAD-BEARING — above `except Exception`
                # (`DuckDBFactTermStoreLockedError` is a `ValueError` subclass, so
                # only the generic clause would otherwise take it). Another process
                # holds this KB's fact-term sidecar: a host/environment condition
                # that clears when that process lets go, exactly the category the
                # ConfigCorrupt clause above refuses to charge to the content.
                #
                # This handler writes NOTHING, and here that is not merely
                # conservative — `process_extraction_job` has already rolled the job
                # back to `pending` so the next pass RESUMES it, and
                # `fail_extraction_job` would overwrite that with `failed`. That
                # overwrite no longer costs what it used to: a `failed` job whose
                # chunks are all `pending`/`done` has no failed chunk for planning
                # to retry, and planning now continues it on the strength of the
                # chunks it finished instead of rebuilding from scratch (#524), so
                # the LLM is not paid twice for them. What the overwrite still
                # costs is the truth. `pending` is what this job is — nothing is
                # running and nothing failed the content — while `failed` files a
                # condition of the host as this job's own failure, in the job row,
                # in the `extraction_job_failed` event beside it, and on the
                # Sources page that reads them. Log and leave it.
                logger.warning(
                    "extraction job %s paused (fact-term store locked by another "
                    "process): %s",
                    job_id,
                    exc,
                )
            except LLMError as e:
                # GUARDED LIKE THE CLAUSE BELOW, though only that one is known to
                # need it. This clause is live — `_extraction_schema_hint(cfg)`
                # turns an unreadable prompt override into an `LLMError` (#539),
                # and `process_extraction_job` raises one for a missing job or
                # source — but on every such path the job is still `pending`, so
                # the guard permits the write and changes nothing. No production
                # path raises an `LLMError` with the job already `done`: the chunk
                # loop swallows `LLMError` through `_release_claimed_chunk`, and
                # the two calls that run after `process_extraction_job` returns
                # carry their own guards. It is guarded anyway because "does this
                # call still own this job?" is not a question the exception TYPE
                # answers, and these two clauses are one decision in two halves —
                # `LLMError` is a `RuntimeError` subclass, so they are adjacent,
                # not alternatives.
                _fail_job_unless_done(f"extraction failed: {_error_cause(e)}")
            # Keep background failures visible: on the job row when this call still
            # owns the job, in the log when it does not.
            except Exception as e:  # noqa: BLE001
                _fail_job_unless_done(f"analysis failed: {_error_cause(e)}")

        threading.Thread(
            target=run,
            name=f"verinote-source-extract-{job_id}",
            daemon=True,
        ).start()

    def _source_file_path(source_path: str, root: Path) -> Path:
        path = (root / source_path).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError as e:
            raise OSError(f"refusing to delete source outside KB root: {source_path}") from e
        return path

    def _delete_source_file(source_path: str, root: Path) -> None:
        path = _source_file_path(source_path, root)
        if path.is_file():
            path.unlink()

    def _delete_source_files(paths: set[str], root: Path) -> None:
        for path in paths:
            _source_file_path(path, root)
        for path in sorted(paths):
            _delete_source_file(path, root)

    def _resume_source_extraction_jobs() -> None:
        """Revive interrupted extraction jobs — but never on a halted KB.

        This runs at `create_app()` time, *outside* the request middleware, so the
        middleware's guard does not cover it: before this gate existed, merely
        launching `verinote ui` against a halted KB with a pending job wrote to it
        (the worker raised, and `except Exception` "helpfully" marked the job
        `failed`) — a write to a halted KB with zero HTTP requests made (#194).

        Same predicate as the middleware and the CLI dispatch: one judgement,
        three enforcement points.

        A job left `running` by a crash is rolled back to `pending` before it is
        restarted, because `process_extraction_job` now claims only a `pending`
        job (#240). SCOPE BOUNDARY (#242): DB state alone cannot tell a crashed
        zombie from a job a DIFFERENT live process genuinely owns — SQLite has no
        row-level liveness signal — so in that rare case this rollback still
        resets that live job's in-flight chunk (exactly as today's unconditional
        resume already does; not a regression introduced here). Closing it needs a
        liveness lease (owner token + heartbeat, or a staleness threshold on
        `updated_at`) and is filed as a follow-up, not solved here.
        """
        if app.state.store is None or app.state.cfg is None:
            return
        try:
            assert_settings_intact(app.state.cfg)
            assert_credentials_intact(app.state.cfg)
        except (ConfigCorruptError, CredentialsCorruptError) as exc:
            # Same shape as the policy gate below: launching against a corrupt
            # config must not resume a job that would reach the cloud default with
            # zero HTTP requests made. Log and touch nothing (#269).
            logger.warning("not resuming extraction jobs: %s", exc)
            return
        try:
            assert_writable(app.state.store)
        except PolicyMissingError as exc:
            logger.warning("not resuming extraction jobs: %s", exc)
            return
        jobs = app.state.store.source_extraction_jobs()
        latest_job_ids = latest_source_job_ids(jobs)
        for job in jobs:
            if not is_live_extraction_job(job, latest_job_ids):
                continue
            if job["status"] == "running":
                app.state.store.rollback_extraction_job(
                    int(job["id"]), "Resuming analysis interrupted by a restart."
                )
            _start_source_extraction(int(job["id"]), app.state.cfg)

    def _start_repair_job(job_id: int, cfg: Config) -> None:
        """Schedule durable repair work; the request path never reaches an LLM."""
        assert_settings_intact(cfg)
        assert_credentials_intact(cfg)

        with app.state.repair_scheduler_lock:
            if job_id in app.state.repair_scheduled:
                return
            app.state.repair_scheduled.add(job_id)

        def run() -> None:
            try:
                with Store(cfg.db_path) as worker_store:
                    worker_store.init_schema()
                    # Repeat both gates in the worker: settings and policy may have
                    # changed after enqueue but before this daemon gets CPU time.
                    assert_settings_intact(cfg)
                    assert_credentials_intact(cfg)
                    assert_writable(worker_store)
                    client = get_client(cfg)
                    process_repair_job(
                        worker_store, client, job_id=job_id, root=cfg.root,
                        policy_guard=lambda: assert_writable(worker_store),
                    )
            except PolicyMissingError as exc:
                # Do not write a halted KB merely to annotate a background job.
                logger.warning("repair job %s halted (KB policy missing): %s", job_id, exc)
            except (ConfigCorruptError, CredentialsCorruptError) as exc:
                with Store(cfg.db_path) as worker_store:
                    worker_store.init_schema()
                    worker_store.fail_pending_repair_job(
                        job_id, f"repair failed: {_error_cause(exc)}"
                    )
            except LLMError as exc:
                with Store(cfg.db_path) as worker_store:
                    worker_store.init_schema()
                    worker_store.fail_pending_repair_job(
                        job_id, f"repair failed: {_error_cause(exc)}"
                    )
            except Exception as exc:  # noqa: BLE001 - durable UI-visible worker error
                logger.exception("repair job %s failed", job_id)
                with Store(cfg.db_path) as worker_store:
                    worker_store.init_schema()
                    worker_store.fail_pending_repair_job(job_id, f"repair failed: {_short_error(exc)}")
            finally:
                with app.state.repair_scheduler_lock:
                    app.state.repair_scheduled.discard(job_id)

        threading.Thread(
            target=run, name=f"verinote-question-repair-{job_id}", daemon=True
        ).start()

    def _resume_repair_jobs() -> None:
        """Schedule only pending or expired-lease repair jobs, never live owners."""
        if app.state.store is None or app.state.cfg is None:
            return
        try:
            assert_settings_intact(app.state.cfg)
            assert_credentials_intact(app.state.cfg)
            assert_writable(app.state.store)
        except (ConfigCorruptError, CredentialsCorruptError, PolicyMissingError) as exc:
            logger.warning("not resuming repair jobs: %s", exc)
            return
        for job in app.state.store.repair_jobs_to_resume():
            _start_repair_job(int(job["id"]), app.state.cfg)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        return _dashboard(request)

    @app.post("/kb/select", response_class=HTMLResponse)
    def select_kb(request: Request, root: str = Form(...)):
        path = root.strip()
        if not path:
            return _kb_select(request, error="KB directory is required", status_code=400)
        try:
            _open_root(Path(path))
        except ConfigCorruptError as e:
            return _kb_select(
                request,
                error=f"refused to open KB — its config.json is corrupt: {e}",
                status_code=400,
            )
        except (KBLocationError, OSError, sqlite3.OperationalError) as e:
            return _kb_select(request, error=f"could not open KB: {e}", status_code=400)
        return RedirectResponse("/", status_code=303)

    @app.get("/sources", response_class=HTMLResponse)
    def sources_page(request: Request):
        return _sources(request)

    @app.post("/sources", response_class=HTMLResponse)
    async def upload_source(request: Request, file: UploadFile = File(...)):
        store = _active_store()
        cfg = _active_cfg()
        filename = Path(file.filename or "").name
        raw = await file.read()
        try:
            text, kind = ingest_bytes(raw, filename)
        except IngestError as e:
            return _sources(request, error=str(e), status_code=400)

        result = store_source(store, cfg.root, filename, raw, text, kind)
        source = store.get_source_by_path(result["citation"])
        if source is None:
            return _sources(
                request,
                error=f"source registration failed: {result['citation']}",
                status_code=500,
            )
        job_id = create_chunked_extraction_job(
            store,
            source_id=int(source["id"]),
            artifact_id=int(result["artifact_id"]),
            source_text=result["text"],
            provider=cfg.provider,
            model=cfg.model,
            chunk_chars=cfg.extraction_chunk_chars,
            chunk_overlap_chars=cfg.extraction_chunk_overlap_chars,
        )
        _start_source_extraction(job_id, app.state.cfg)
        return RedirectResponse("/sources", status_code=303)

    @app.post("/sources/jobs/{job_id}/retry", response_class=HTMLResponse)
    def retry_source_job(request: Request, job_id: int):
        # The atomic claim-for-retry inside the worker resets the failed chunks AND
        # takes ownership in one locked step, so a concurrent `verinote sync`
        # auto-retry on the same job_id cannot collide: whoever wins the CAS owns
        # it and the loser backs off via ExtractionJobBusyError (handled in the
        # worker above). `retry_max_attempts=None` makes this a human override that
        # resets EVERY failed chunk regardless of attempt count, unlike the capped
        # auto-retry — the escape hatch for a job the sync loop has given up on (#323).
        _start_source_extraction(
            job_id, _active_cfg(), retry=True, retry_max_attempts=None
        )
        return RedirectResponse("/sources", status_code=303)

    @app.post("/sources/{source_id}/reanalyze", response_class=HTMLResponse)
    def reanalyze_source(request: Request, source_id: int):
        store = _active_store()
        cfg = _active_cfg()
        source = store.get_source(source_id)
        if source is None:
            return _sources(request, error="source not found", status_code=404)
        jobs = store.source_extraction_jobs()
        latest_job_ids = latest_source_job_ids(jobs)
        # Only a LIVE job blocks re-analysis. A superseded `pending` row is not an
        # analysis in progress, and treating it as one wedges this button shut for
        # the one source whose analysis most needs redoing.
        if any(
            int(job["source_id"]) == source_id
            and is_live_extraction_job(job, latest_job_ids)
            for job in jobs
        ):
            return _sources(
                request,
                error=f"analysis already running for {source['path']}",
                status_code=409,
            )
        artifact = store.latest_source_text_artifact(source_id)
        if artifact is None:
            return _sources(
                request,
                error=f"source has no extraction text artifact: {source['path']}",
                status_code=400,
            )
        try:
            artifact_path = _source_file_path(str(artifact["path"]), cfg.root)
            source_text = artifact_path.read_text(encoding="utf-8")
        except OSError as e:
            return _sources(
                request,
                error=f"could not read source artifact: {e}",
                status_code=500,
            )

        store.clear_source_analysis(source_id)
        job_id = create_chunked_extraction_job(
            store,
            source_id=source_id,
            artifact_id=int(artifact["id"]),
            source_text=source_text,
            provider=cfg.provider,
            model=cfg.model,
            chunk_chars=cfg.extraction_chunk_chars,
            chunk_overlap_chars=cfg.extraction_chunk_overlap_chars,
        )
        _start_source_extraction(job_id, cfg)
        return RedirectResponse("/sources", status_code=303)

    @app.post("/sources/{source_id}/accept-all", response_class=HTMLResponse)
    def accept_all_source_facts(request: Request, source_id: int):
        store = _active_store()
        accepted = store.accept_review_facts_for_source(source_id)
        # Bulk-confirming a source can corroborate facts elsewhere; the redirect
        # reloads the page so no HX-Refresh header is needed here. `accepted` is
        # the same transition test the single-fact routes apply: a source with
        # nothing left in the review tier confirms nothing, so the POST decided
        # nothing and the rule stays out of it.
        if accepted:
            # The policy files are read here rather than at the top of the
            # route: a source with nothing left in the review tier never runs
            # the pass, so it owes no read (#570).
            _maybe_apply_auto_accept(policy_error=_trust_policy_failure(store))
        return RedirectResponse("/sources", status_code=303)

    @app.post("/sources/{source_id}/delete", response_class=HTMLResponse)
    def delete_source(request: Request, source_id: int):
        store = _active_store()
        cfg = _active_cfg()
        source = store.get_source(source_id)
        paths = {source["path"]} if source is not None else set()
        paths.update(row["path"] for row in store.source_artifacts(source_id))
        try:
            for path in paths:
                _source_file_path(path, cfg.root)
        except OSError as e:
            return _sources(request, error=f"source removal failed: {e}", status_code=500)
        source = store.delete_source(source_id)
        if source is not None:
            try:
                _delete_source_files(paths, cfg.root)
            except OSError as e:
                return _sources(
                    request,
                    error=f"source deleted, but file removal failed: {e}",
                    status_code=500,
                )
        return RedirectResponse("/sources", status_code=303)

    @app.get("/review", response_class=HTMLResponse)
    def review(
        request: Request,
        filter: str = "needs-human-decision",
        page: str = "1",
        page_size: str = str(DEFAULT_REVIEW_PAGE_SIZE),
        sort: str = "newest",
    ):
        store = _active_store()
        active_filter = _active_review_filter(filter)
        policy_error = _trust_policy_failure(store)
        if policy_error is not None and active_filter != "needs-human-decision":
            # #570. Every filter but `needs-human-decision` — `unsupported`,
            # `single-source`, `corroborated`, `conflicted` — selects facts BY the value that
            # could not be computed: `_review_page` runs `fact_trust_summary`
            # over the whole queue and keeps the ids whose labels match. So the
            # row set, the total and the pager are themselves trust-derived, not
            # just the badges. There is no degraded list to show. Falling back
            # to the default filter would answer a question the user did not
            # ask; rendering an empty one would be a false statement about which
            # facts carry this label.
            #
            # This RETURNS rather than setting a flag the code below reads. With
            # `page_data = None` the `[int(f["id"]) for f in page_data.rows]`
            # argument to `accept_recommendations_for` raises `AttributeError` —
            # a 500 with nothing to do with the policy files.
            #
            # The filter nav is how the user gets back to a page that works, so
            # it survives — and its hrefs must carry the sort and page size the
            # user already chose, normalized exactly as a healthy render
            # normalizes them. `ReviewQueuePage.from_rows` is that normalizer and
            # it is pure arithmetic over the query string; re-deriving the two
            # values here would let this nav drift from the healthy one.
            nav = ReviewQueuePage.from_rows(
                rows=[], total=0, page=page, page_size=page_size, sort=sort
            )
            return templates.TemplateResponse(
                request,
                "review.html",
                {
                    "queue": None,
                    "active_filter": active_filter,
                    "filters": _review_filter_links(
                        active_filter, nav.sort, nav.page_size
                    ),
                    "pager": None,
                    "policy_error": policy_error,
                },
            )
        page_data = _review_page(store, active_filter, page, page_size, sort)
        # #570. On the default filter the ROWS are real: `store.review_queue_page`
        # reads no policy file. Measured by spying every module that binds
        # `store_relation_aliases` — a healthy `/review` reads it from
        # `_trust_policy_failure` (this route's own guard, above),
        # `acceptance._engine`, `trust.fact_trust_summary` and
        # `corroboration.store_single_valued_conflicts`, and from nowhere else.
        # The readers are named rather than counted on purpose: the count moves
        # whenever a guard is added, and what this comment needs to say is that
        # the QUEUE QUERY is not among them. So the queue is the KB's own and
        # only its trust signals are withheld. `store_typed_relations` is bound
        # in the same modules and read by the last three of those four, so #585
        # broadening this route's guard does not widen what is withheld — the
        # queue query reads neither file.
        # `accept_recommendations_for` is the opposite: it builds its engine off
        # both policy files before it looks at a single id, so it raises even for
        # an empty id list. Skipping it and passing `{}` is what keeps this route
        # up.
        recommendations = (
            {}
            if policy_error is not None
            else accept_recommendations_for(
                store, [int(f["id"]) for f in page_data.rows]
            )
        )
        rows = [
            _fact_row_context(f, recommendations, policy_error=policy_error)
            for f in page_data.rows
        ]
        return templates.TemplateResponse(
            request,
            "review.html",
            {
                "queue": rows,
                "active_filter": active_filter,
                "filters": _review_filter_links(
                    active_filter, page_data.sort, page_data.page_size
                ),
                "pager": _review_pager(active_filter, page_data),
                "policy_error": policy_error,
            },
        )

    @app.get("/workbench", response_class=HTMLResponse)
    def workbench(request: Request):
        # #570. `trust_workbench` reads the alias file in its first statement
        # and the typed file in its second, and its whole return value is the
        # page. `None` rather than an empty
        # workbench: `{% if workbench.corroborated %}` is falsy either way, so an
        # empty value renders "No facts are corroborated by multiple distinct
        # sources." and "No source-backed single-valued conflicts." — two claims
        # about a KB nothing analysed. Only `None` lets the template tell "not
        # computed" from "computed, and empty".
        store = _active_store()
        policy_error = _trust_policy_failure(store)
        return templates.TemplateResponse(
            request,
            "workbench.html",
            {
                "workbench": None if policy_error is not None else trust_workbench(store),
                "policy_error": policy_error,
            },
        )

    @app.post("/facts/{fact_id}/toggle", response_class=HTMLResponse)
    def toggle(request: Request, fact_id: int):
        _actionable_fact_or_error(fact_id)
        toggled = _active_store().toggle_review(fact_id)
        _mark_saved_fact_decision(request, "toggle", toggled)
        # A demotion parks the fact in exactly the tier auto-accept promotes
        # from, so an unrestricted pass would undo the user's click inside their
        # own request. The demotion is the decision; the rule may act on the
        # siblings it unblocks, but not on this fact.
        demoted = (
            toggled.changed
            and toggled.fact is not None
            and toggled.fact["status"] in review_statuses()
        )
        return _row_after_decision(
            request,
            toggled.fact,
            fact_id,
            rule_may_act=not demoted,
            decided=toggled.changed,
        )

    @app.post("/facts/{fact_id}/accept", response_class=HTMLResponse)
    def accept(request: Request, fact_id: int):
        _actionable_fact_or_error(fact_id)
        accepted = _active_store().accept_fact(fact_id)
        _mark_saved_fact_decision(request, "accept", accepted)
        return _row_after_decision(
            request, accepted.fact, fact_id, decided=accepted.changed
        )

    @app.post("/facts/{fact_id}/reject", response_class=HTMLResponse)
    def reject(request: Request, fact_id: int):
        # Reject runs auto-accept too: removing a fact's support (or freeing a
        # single-valued slot it conflicted on) also reshapes corroboration, so
        # keeping the trigger here matches the other decision routes.
        _actionable_fact_or_error(fact_id)
        rejected = _active_store().reject_fact(fact_id)
        _mark_saved_fact_decision(request, "reject", rejected)
        return _row_after_decision(
            request, rejected.fact, fact_id, decided=rejected.changed
        )

    @app.get("/facts/{fact_id}/edit", response_class=HTMLResponse)
    def edit_fact(request: Request, fact_id: int):
        fact = _actionable_fact_or_error(fact_id)
        return templates.TemplateResponse(
            request,
            "partials/fact_edit.html",
            _fact_edit_context(fact),
        )

    @app.get("/facts/{fact_id}/row", response_class=HTMLResponse)
    def fact_row(request: Request, fact_id: int):
        # Re-render the read-only row (used to cancel an inline edit).
        store = _active_store()
        return _row(
            request,
            store.get_fact(fact_id),
            policy_error=_trust_policy_failure(store),
        )

    @app.post("/facts/{fact_id}/amend", response_class=HTMLResponse)
    def amend_fact(
        request: Request,
        fact_id: int,
        subject: str = Form(...),
        relation: str = Form(...),
        object: str = Form(...),
        subject_kind: str = Form(...),
        relation_kind: str = Form(...),
        object_kind: str = Form(...),
        note: str = Form(""),
    ):
        _actionable_fact_or_error(fact_id)
        try:
            subject_value = _fact_input(subject, subject_kind)
            relation_value = _fact_input(relation, relation_kind)
            object_value = _fact_input(object, object_kind)
        except ValueError as e:
            return templates.TemplateResponse(
                request,
                "partials/fact_edit.html",
                _fact_edit_context(_active_store().get_fact(fact_id), error=str(e)),
                status_code=400,
            )
        try:
            amended = _active_store().amend_fact(
                fact_id,
                subject=subject_value,
                relation=relation_value,
                obj=object_value,
                note=note,
            )
        except TerminalFactError:
            # #311: the fact was rejected, so its content is frozen. Reachable
            # through a TOCTOU window -- a reject landing between this route's
            # `_actionable_fact_or_error` pre-check and the `store.amend_fact`
            # call above -- and NOT from a stale edit form: that pre-check reads
            # the fact's status on THIS request, so a form left open while
            # someone else rejects it gets a plain 400 (measured). The sentence
            # this replaces predates `_actionable_fact_or_error` and was true
            # until it landed.
            # Re-render the read-only row at 200 rather than an error at 4xx:
            # htmx's default responseHandling does not swap 4xx, so an error
            # status would leave the stale edit form on screen still offering a
            # save that cannot succeed. The row it swaps in says "rejected -- no
            # further action", which is both the state and the explanation.
            #
            # By the same reasoning the validation-error path above, which
            # re-renders the edit form at 400, does not swap either and so shows
            # the user nothing. That predates this change and is left alone here
            # rather than fixed in passing, but it is the same bug.
            #
            # This is `amend_fact`'s SECOND row-rendering exit; the success path
            # below renders through `_row_after_decision`, which checks the
            # policy files itself. Guarding only that one leaves this path
            # 500ing on a broken policy file while a naive amend test passes
            # (#570).
            store = _active_store()
            return _row(
                request,
                store.get_fact(fact_id),
                policy_error=_trust_policy_failure(store),
            )
        # The rule may act on the amended fact itself, unlike a toggle demotion.
        # An amend decides the fact's content, not its tier: correcting a term so
        # it finally matches a second source's wording *is* corroboration
        # arriving, and promoting on it is the recommender working as intended.
        return _row_after_decision(
            request, amended.fact, fact_id, decided=amended.changed
        )

    @app.get("/facts/{fact_id}/provenance", response_class=HTMLResponse)
    def provenance(request: Request, fact_id: int):
        store = _active_store()
        fact = store.get_fact(fact_id)
        # This route calls `fact_trust_summary` DIRECTLY — it does not go
        # through `_fact_row_context`, so the `policy_error` threaded through
        # that helper never reaches here and this route computes its own (#570
        # trap 1). Withholding `trust` here is inseparable from the
        # `{% if trust %}` blocks in `provenance.html`: that template writes
        # `trust.support.source_count`, and Jinja raises `UndefinedError` on a
        # two-deep attribute of `None`, so guarding this line alone turns one
        # 500 into a different 500. Route and template are one guard.
        policy_error = _trust_policy_failure(store)
        trust = fact_trust_summary(store, fact_id) if fact and policy_error is None else None
        run = store.get_run(fact["run_id"]) if fact and fact["run_id"] else None
        job = (
            store.get_extraction_job_detail(fact["job_id"])
            if fact and fact["job_id"]
            else None
        )
        return templates.TemplateResponse(
            request,
            "provenance.html",
            {
                "f": _fact_view(fact),
                "trust": trust,
                "policy_error": policy_error,
                "run": run,
                "job": job,
                "log": store.fact_log(fact_id) if fact else [],
            },
        )

    def _questions(request: Request, *, error: str | None = None, status_code: int = 200):
        if app.state.store is None:
            return _kb_select(request, error=error, status_code=status_code)
        store = _active_store()
        rep = verify(store)
        page_error = error
        if page_error is None:
            # Ask the thing that owns the answer, never the report's prose: a
            # finding string is human-readable output, not a state field. (The
            # missing-policy state is handled by the guard, which never routes
            # here.) The query policy's own error type is what surfaces below.
            try:
                load_query(store)
            except CorroborationPolicyError as exc:
                page_error = f"policy error: {exc}"
        return templates.TemplateResponse(
            request,
            "questions.html",
            {
                "questions": [question_outcome_view(q) for q in store.questions()],
                "answers": rep.answers,
                "error": page_error,
                "repair_job": store.latest_repair_job(),
            },
            status_code=status_code,
        )

    @app.get("/questions", response_class=HTMLResponse)
    def questions_page(request: Request):
        return _questions(request)

    def _ask(
        request: Request,
        *,
        question: str = "",
        result=None,
        error: str | None = None,
        status_code: int = 200,
    ):
        if app.state.store is None:
            return _kb_select(request, error=error, status_code=status_code)
        return templates.TemplateResponse(
            request,
            "ask.html",
            {"question": question, "result": result, "error": error},
            status_code=status_code,
        )

    @app.get("/ask", response_class=HTMLResponse)
    def ask_page(request: Request):
        return _ask(request)

    @app.post("/ask", response_class=HTMLResponse)
    def ask_submit(request: Request, question: str = Form(...)):
        store = _active_store()
        cfg = _active_cfg()
        try:
            client = get_client(app.state.cfg)
        except LLMError as e:
            return _ask(request, question=question, error=f"ask failed: {e}", status_code=502)
        result = ask_question(store, client, root=cfg.root, question=question)
        return _ask(request, question=question, result=result)

    @app.post("/questions", response_class=HTMLResponse)
    def add_question(request: Request, text: str = Form(...)):
        _active_store().add_question(text)
        return RedirectResponse("/questions", status_code=303)

    @app.post("/questions/{question_id}/delete", response_class=HTMLResponse)
    def delete_question(request: Request, question_id: int):
        store = _active_store()
        store.delete_question(question_id)
        write_query_file(store, _active_cfg().root)
        return RedirectResponse("/questions", status_code=303)

    @app.post("/questions/translate", response_class=HTMLResponse)
    def translate(request: Request):
        store = _active_store()
        cfg = _active_cfg()
        try:
            client = get_client(app.state.cfg)
        except LLMError as e:
            _fail_pending_translations(store, cfg, e)
            return RedirectResponse("/questions", status_code=303)
        translate_questions(store, client, root=cfg.root)
        return RedirectResponse("/questions", status_code=303)

    @app.post("/questions/repair", response_class=HTMLResponse)
    def repair(request: Request):
        store = _active_store()
        cfg = _active_cfg()
        # This is intentionally the only synchronous validation. Constructing a
        # client can touch provider configuration; LLM work belongs to the worker.
        assert_settings_intact(cfg)
        assert_credentials_intact(cfg)
        job, _created = store.enqueue_repair_job(provider=cfg.provider, model=cfg.model)
        if job["status"] == "pending":
            _start_repair_job(int(job["id"]), cfg)
        return RedirectResponse("/questions", status_code=303)

    @app.get("/report", response_class=HTMLResponse)
    def report(request: Request):
        store = _active_store()
        rep = verify(store)
        try:
            trace = report_trace(store)
        except PolicyMissingError:
            # The report itself already carries the policy_missing error; the
            # trace needs the same (lost) policy, so it has nothing to say.
            trace = ReportTrace(
                answers=(), excluded_review_count=0, excluded_by_status=()
            )
        return templates.TemplateResponse(
            request,
            "report.html",
            {"rep": rep, "trace": trace},
        )

    @app.get("/analytics", response_class=HTMLResponse)
    def analytics(request: Request):
        from verinote.store.analytics import compute

        return templates.TemplateResponse(request, "analytics.html", {"a": compute(_active_cfg().db_path)})

    def _model_field_context(
        *, provider: str, model: str, base_url: str, lazy: bool, custom: bool = False
    ) -> dict:
        """Resolve the Model field's state for one (provider, base URL) pair.

        `lazy=True` returns the not-yet-loaded state without touching the
        network; the partial then fetches itself. Only the eager call reaches
        the provider, and only for a provider whose models are enumerable from
        the endpoint the user pointed at.

        `models` distinguishes three outcomes the UI must not conflate: a
        sequence of ids (what that endpoint listed, possibly empty), or `None`
        with `models_error`
        set (the endpoint could not be asked). `structured_output_ids` carries a
        further distinction alongside it, orthogonal to those three — the subset
        that advertises structured output, or `None` when the listing does not
        report the property at all (see `ModelListing`), which is why the picker
        only groups for some providers. `ConfigCorruptError` is left to
        propagate to the app-wide halt handler — a corrupt config.json means the
        resolved provider is untrustworthy, and this route would otherwise
        report on a provider the user never chose (#269).

        `aliases` is the other kind of list, and stays a separate key on
        purpose: it is curated from the adapter, never read from a provider, so
        it can never stand in for `models` or be mistaken for discovery.
        `custom` swaps that picker for a text input so a full model id can still
        be entered — a view state, never persisted, so a reload returns to the
        list showing whatever was actually saved.
        """
        listable = provider in MODEL_LISTING_PROVIDERS
        base = {
            "provider": provider,
            "model": model,
            "aliases": MODEL_ALIASES_BY_PROVIDER.get(provider, ()),
            "custom": custom,
            # The URL actually dialled, not the literal "(default)" — so the
            # page names the same endpoint the lister will use, per provider.
            "endpoint": (
                (base_url.strip() or _LISTABLE_DEFAULT_ENDPOINTS[provider]) if listable else ""
            ),
            # A provider with nothing to enumerate must not sit in a lazy state
            # forever: it renders its text input once and never self-fetches.
            "lazy": lazy and listable,
            "models": None,
            "structured_output_ids": None,
            "models_error": None,
        }
        if lazy or not listable:
            return base
        try:
            listing = _list_models_for(app.state.cfg, provider, base_url.strip() or None)
        except LLMError as exc:
            base["models_error"] = str(exc)
            return base
        base["models"] = listing.models
        base["structured_output_ids"] = listing.structured_output_ids
        return base

    @app.get("/settings/model-field", response_class=HTMLResponse)
    def model_field(
        request: Request,
        provider: str = "",
        model: str = "",
        base_url: str = "",
        custom: int = 0,
        provider_changed: int = 0,
    ):
        """The Model field, or — on a provider change — Model plus Base URL.

        `provider_changed=1` is the provider select's request, and it is the only
        one that renders the wrapper. Deliberately this route under a query
        parameter rather than a route of its own: `_ORIGIN_GUARD_GET_PATHS` lists
        this exact path because it dials a caller-supplied `base_url`, and a
        sibling path would fall outside that guard.

        Switching to OpenRouter clears the Base URL, because that field's only
        job is to point verinote at a different endpoint and the endpoint being
        left belongs to the provider being left — `http://localhost:11434` is not
        an OpenRouter endpoint. Only OpenRouter: clearing on every switch would
        wipe an Ollama user's endpoint whenever they touched the provider select.
        Which providers those are is `_BASE_URL_CLEARING_PROVIDERS`, named as a
        set so the import-time check can hold it inside the listable ones — the
        note below has an endpoint to name only for a listable provider.

        The clear lives here and not in the template because it must happen on a
        provider CHANGE and never on a page load. `_settings` renders this same
        context, so a template-side rule would blank a KB whose config.json
        really does name a proxy — the page misreporting the KB's own state.

        And it is announced, not silent: `POST /settings` maps `base_url or None`,
        so Save on an empty field destroys the stored value. `discarded_base_url`
        carries what the field held into the note, which says plainly that this is
        a proposal until Save and what would be dialled instead.
        """
        if app.state.cfg is None:
            return _kb_select(request)
        provider = normalize_provider(provider)
        clears_base_url = bool(provider_changed) and provider in _BASE_URL_CLEARING_PROVIDERS
        discarded_base_url = base_url.strip() if clears_base_url else ""
        if clears_base_url:
            base_url = ""
        context = _model_field_context(
            provider=provider,
            model=model,
            base_url=base_url,
            lazy=False,
            custom=bool(custom),
        )
        if not provider_changed:
            return templates.TemplateResponse(request, "partials/model_field.html", context)
        return templates.TemplateResponse(
            request,
            "partials/provider_fields.html",
            {
                **context,
                "base_url": base_url,
                "discarded_base_url": discarded_base_url,
                "provider_label": PROVIDER_LABELS.get(provider, provider),
            },
        )

    def _settings(request: Request, *, test_result=None, error=None, status_code=200):
        c = app.state.cfg
        if c is None:
            return _kb_select(request)
        theme_editable = True
        # Same treatment as the theme, and for the same reason: `/settings/root/persist`
        # also writes machine-wide state and is NOT exempt from the policy guard,
        # so "it writes outside the KB" is not this repo's rule. Disabling the
        # control beats a live form that 409s about an unrelated KB.
        credentials_editable = True
        if app.state.store is not None:
            try:
                assert_writable(app.state.store)
            except PolicyMissingError:
                theme_editable = False
                credentials_editable = False
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "providers": PROVIDERS,
                "provider_labels": PROVIDER_LABELS,
                "provider": c.provider,
                "provider_label": PROVIDER_LABELS.get(c.provider, c.provider),
                "model": c.model,
                "base_url": c.base_url or "",
                "extraction_chunk_chars": c.extraction_chunk_chars,
                "extraction_chunk_overlap_chars": c.extraction_chunk_overlap_chars,
                "extraction_max_facts_per_chunk": c.extraction_max_facts_per_chunk,
                "auto_accept_recommendations": c.auto_accept_recommendations,
                "root": c.root,
                # Read from disk, not from `c`: `app.state.cfg` is a snapshot, and
                # the page must report what is stored now. Never the value — only
                # which source answered.
                # Both from the SAME fresh read: rows disk-fresh while the banner
                # came from the snapshot meant the one state that most needs the
                # recovery link — "unknown" — could render without it.
                **_credentials_context(c.provider),
                "credentials_editable": credentials_editable,
                "connection_test_enabled": c.provider in TESTABLE_PROVIDERS,
                **_relation_aliases_context(),
                # The Model field starts as the plain text input and, for a
                # model-enumerable provider, lazily swaps itself for the picker
                # (`hx-trigger="load"`). Enumerating eagerly here would put a
                # network call on the critical path of the one page that is also
                # the recovery surface for a corrupt config and a halted policy.
                **_model_field_context(
                    provider=c.provider, model=c.model, base_url=c.base_url or "", lazy=True
                ),
                "test_result": test_result,
                "error": error,
                # /settings is deliberately reachable during the halt (it is the
                # recovery page). When config.json is corrupt the provider shown
                # above is the built-in default, NOT the user's saved choice, so
                # warn instead of silently presenting it as chosen (#269).
                "settings_error": c.settings_error,
                "app_themes": APP_THEMES,
                "theme_editable": theme_editable,
            },
            status_code=status_code,
        )

    def _credentials_context(active_provider: str) -> dict:
        """The API-keys section, all from one read of the file."""
        stored, error = _read_credentials()
        rows = _provider_key_rows(stored, error)
        # The read error and the *refusal* are different facts. An unreadable
        # file only halts a provider whose key it would have decided, so with an
        # environment key in place nothing is refused — reporting the raw read
        # error as a refusal put a red "extraction is halted" alert on a setup
        # that was working. `halting` is the claim; `credentials_error` is the
        # detail, shown either way because an unreadable file is worth saying.
        return {
            "provider_keys": rows,
            "credentials_error": error,
            # The ACTIVE provider decides the claim: `any(unknown)` is true as
            # soon as some other provider would be affected, which turned a
            # working setup into a red "extraction is halted" alert. The other
            # providers' own rows already say `unknown` for themselves.
            "credentials_halting": any(
                row["provider"] == active_provider and row["state"] == "unknown"
                for row in rows
            ),
        }

    def _provider_key_rows(stored: dict, error: str | None) -> list[dict]:
        """One row per provider: which source supplies its key, never the key.

        `shadowed` exists because "set from the environment" alone would hide
        that a key the user saved is being overridden — they would edit the
        saved one and see nothing change.
        """
        rows = []
        for provider in PROVIDERS:
            state, _ = api_key_source(provider, stored, error)
            rows.append(
                {
                    "provider": provider,
                    "label": PROVIDER_LABELS.get(provider, provider),
                    "state": state,
                    "env_var": provider_key_env_var(provider),
                    "shadowed": state == "env_provider" and bool(stored.get(provider)),
                    "stored": bool(stored.get(provider)),
                }
            )
        return rows

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        return _settings(request)

    @app.get("/prompts", response_class=HTMLResponse)
    def prompts_page(request: Request, prompt: str = "extraction"):
        return _prompts_page(request, prompt_id=prompt)

    @app.post("/prompts", response_class=HTMLResponse)
    def save_prompt_route(
        request: Request,
        prompt_id: str = Form(...),
        prompt_text: str = Form(""),
    ):
        cfg = _active_cfg()
        try:
            save_prompt_override(cfg.root, prompt_id, prompt_text)
        except PromptError as exc:
            return _prompts_page(
                request,
                prompt_id=prompt_id,
                prompt_text=prompt_text,
                error=str(exc),
                status_code=400,
            )
        except Exception as exc:  # noqa: BLE001 - a KB tree the user can chmod fails in ways PromptError does not name
            # `save_prompt_override` does filesystem work — `mkdir` then
            # `write_text` — and the `OSError` family that raises is outside
            # `PromptError`'s hierarchy, so a write the operator's own mode bits
            # refused left this handler as an unhandled exception: a bare error
            # response, no page, and nothing saying which file or why (#545).
            #
            # Broad rather than a type list, per the house form of
            # `_prompts_page` above and of
            # `verinote/pipeline/extract.py::_relation_aliases_or_error`: the
            # ways a directory the user can chmod fails are not a set this route
            # can enumerate, and every member of it is better answered with a
            # page that names the file than with a traceback. `Exception` and
            # never `BaseException`, so `KeyboardInterrupt`, `SystemExit` and
            # `CancelledError` still travel.
            #
            # BELOW the narrow `except PromptError`, which keeps its 400: empty
            # text, a missing required placeholder and an unknown `prompt_id`
            # are refusals of the request, not failures of the disk, and folding
            # them in here would relabel all three. Read
            # `verinote/prompts/library.py`: in `save_prompt_override`,
            # `prompt_definition`, the empty check and `_validate_prompt_text`
            # all run BEFORE `mkdir`, and nothing after it raises `PromptError`
            # — so the narrow clause above cannot today take a write failure and
            # report it as a validation refusal. That is a property of the
            # current library, not a guarantee: add a `PromptError` after the
            # write and this ordering needs revisiting.
            #
            # 500 rather than 400: the user's text was accepted and the disk
            # said no, so repeating the identical request once the mode bits are
            # fixed is exactly the right thing to do — which is what 4xx denies.
            #
            # `cfg = _active_cfg()` stays ABOVE the `try`, and the body stays
            # the single library call. Its `RuntimeError("no active KB")` is an
            # application-state bug; caught here it would render as "could not
            # be saved to …: no active KB", blaming the filesystem for it.
            #
            # The path is named here rather than left to `str(exc)`: a failing
            # `mkdir` names only the directory, and an exception outside
            # `OSError` names nothing at all.
            return _prompts_page(
                request,
                prompt_id=prompt_id,
                prompt_text=prompt_text,
                error=(
                    f"prompt {prompt_id} could not be saved to "
                    f"{prompt_override_path(cfg.root, prompt_id)}: {exc}"
                ),
                status_code=500,
            )
        return RedirectResponse(f"/prompts?{urlencode({'prompt': prompt_id})}", status_code=303)

    @app.post("/prompts/reset", response_class=HTMLResponse)
    def reset_prompt_route(request: Request, prompt_id: str = Form(...)):
        cfg = _active_cfg()
        try:
            delete_prompt_override(cfg.root, prompt_id)
        except PromptError as exc:
            return _prompts_page(
                request,
                prompt_id=prompt_id,
                error=str(exc),
                status_code=400,
            )
        except Exception as exc:  # noqa: BLE001 - `exists()`/`unlink()` fail the ways a save does
            # The same clause and the same reasoning as `save_prompt_route`'s
            # above: why it is broad, why it sits below the narrow one, why the
            # status is what it is, why `_active_cfg()` stays outside the `try`,
            # and why the path is named rather than left to `str(exc)`.
            #
            # Reset is not the working half of this pair. `delete_prompt_override`
            # calls `path.exists()` before `path.unlink()`, and under a directory
            # the operator restricted BOTH raise — `Path.exists()` propagates
            # `EACCES` rather than answering False, which is what took the reset
            # POST down from a `0o000` prompts directory.
            #
            # No `prompt_text`: this route carries none, so what its failure
            # page can carry is decided by what `get_prompt` makes of the
            # override, and that has three outcomes, not two. Loads: the full
            # editor, banner above it. Cannot be READ: `reset_only` renders the
            # section that deletes it, banner above it. Reads but fails
            # VALIDATION: `_prompts_page` takes its `except PromptError` branch,
            # where `prompt` is None and that branch passes `reset_only=False`
            # outright — `_override_is_unreadable` is the `except Exception`
            # branch's gate and is never consulted here — so the page is the
            # banner and the prompt selector and no control at all.
            # `test_a_reset_that_cannot_unlink_is_a_page_not_a_crash` pins the
            # first two shapes, one per param;
            # `test_a_failed_reset_over_an_invalid_override_offers_no_control`
            # pins the third.
            #
            # That third page is deliberately left short of a repair control.
            # Offering one for a readable-but-invalid override is #546's whole
            # subject, and the line it has to change is that hardcoded
            # `reset_only=False` — not this clause, and not the predicate. What
            # it cannot do is flip that literal to True, because `get_prompt`
            # raises the same `PromptError` when the PACKAGED default is what
            # fails validation, where a reset would delete the user's file and
            # fix nothing. What this change does deliver there is the diagnosis:
            # measured against `2c96317`, the same request answered a bare 500
            # carrying no page, no banner and not even the selector.
            return _prompts_page(
                request,
                prompt_id=prompt_id,
                error=(
                    f"prompt {prompt_id} override could not be deleted from "
                    f"{prompt_override_path(cfg.root, prompt_id)}: {exc}"
                ),
                status_code=500,
            )
        return RedirectResponse(f"/prompts?{urlencode({'prompt': prompt_id})}", status_code=303)

    @app.post("/settings", response_class=HTMLResponse)
    def save_settings_route(
        request: Request,
        provider: str = Form(...),
        model: str = Form(""),
        base_url: str = Form(""),
        extraction_chunk_chars: int = Form(300),
        extraction_chunk_overlap_chars: int = Form(40),
        extraction_max_facts_per_chunk: int = Form(8),
        auto_accept_recommendations: str | None = Form(None),
    ):
        cfg = _active_cfg()
        save_settings(
            cfg.root,
            provider=provider,
            model=model,
            base_url=base_url or None,
            extraction_chunk_chars=extraction_chunk_chars,
            extraction_chunk_overlap_chars=extraction_chunk_overlap_chars,
            extraction_max_facts_per_chunk=extraction_max_facts_per_chunk,
            auto_accept_recommendations=auto_accept_recommendations == "on",
        )
        # reload from the app's own root so the change takes effect on next sync
        app.state.cfg = Config.for_root(cfg.root)
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/credentials", response_class=HTMLResponse)
    def save_credential_route(
        request: Request,
        provider: str = Form(...),
        api_key: str = Form(""),
    ):
        """Store one provider's key. An empty field means "leave it alone".

        The input renders empty on every load because the key is never echoed
        back, so an empty POST is indistinguishable from "did not touch it";
        treating it as a clear would silently unset a working key. Removing one
        is its own explicit action.
        """
        canonical = normalize_provider(provider)
        if not api_key.strip():
            return _settings(request)
        try:
            save_credential(canonical, api_key)
        except (ValueError, CredentialsCorruptError, OSError) as exc:
            return _settings(request, error=str(exc), status_code=400)
        # Rebuild the snapshot: without this the badge would read the new key
        # from disk while every provider call kept using the old one.
        # Guarded because `_active_cfg()` raises when no KB is selected: the
        # write has already succeeded by then, so raising would report a stored
        # key as a server error.
        if app.state.cfg is not None:
            app.state.cfg = Config.for_root(app.state.cfg.root)
        note = f"Saved an API key for {PROVIDER_LABELS.get(canonical, canonical)}."
        if len(api_key.strip()) < MIN_REDACTABLE_SECRET:
            # Not rejected: a self-hosted gateway token can legitimately be this
            # short. But it will not be redacted from provider error text, which
            # is persisted, so say so rather than let it be discovered later.
            note += (
                f" It is shorter than {MIN_REDACTABLE_SECRET} characters, so it"
                " cannot be redacted from provider error messages, which are"
                " stored in this KB."
            )
        return _settings(request, test_result=note)

    @app.post("/settings/credentials/remove", response_class=HTMLResponse)
    def remove_credential_route(request: Request, provider: str = Form(...)):
        canonical = normalize_provider(provider)
        try:
            removed = delete_credential(canonical)
        except (ValueError, CredentialsCorruptError, OSError) as exc:
            return _settings(request, error=str(exc), status_code=400)
        # Guarded because `_active_cfg()` raises when no KB is selected: the
        # write has already succeeded by then, so raising would report a stored
        # key as a server error.
        if app.state.cfg is not None:
            app.state.cfg = Config.for_root(app.state.cfg.root)
        label = PROVIDER_LABELS.get(canonical, canonical)
        if not removed:
            return _settings(request, test_result=f"No saved key to remove for {label}.")
        # A bare "removed" would read as "this provider has no key now", which is
        # false when the environment still supplies one.
        stored, _error = _read_credentials()
        state, _ = api_key_source(canonical, stored)
        note = f"Removed the saved key for {label}."
        if state in {"env_provider", "env_global"}:
            note += " An environment variable still supplies a key for it."
        return _settings(request, test_result=note)

    @app.post("/settings/theme", response_class=HTMLResponse)
    def save_theme_route(request: Request, theme: str = Form(...)):
        try:
            save_app_theme(theme)
        except ValueError as exc:
            return _settings(request, error=str(exc), status_code=400)
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/relation-aliases", response_class=HTMLResponse)
    def save_relation_aliases(
        request: Request,
        relation_aliases_text: str = Form(""),
        relation_aliases_rendered_unreadable: str | None = Form(None),
    ):
        text = relation_aliases_text.strip()
        if not text and relation_aliases_rendered_unreadable:
            # #555 BLOCKER-2/MUST-FIX-1: `relation_aliases("")` parses cleanly
            # (returns {}), so nothing below would otherwise refuse this submit,
            # and it would reach the `path.exists(): path.unlink()` branch further
            # down. That branch exists for "the user deliberately cleared their
            # aliases" — a real choice when the box started full and readable.
            # But `_relation_aliases_context` (unit C) puts an EMPTY box on the
            # page when the on-disk file could not be read at all (cp949,
            # permission-denied, …) — there the empty box is not a choice, it is
            # the only thing the page could show, and the same submit would
            # delete the user's only copy of their aliases.
            #
            # `relation_aliases_rendered_unreadable` is a hidden field the
            # template only emits when THIS render's box was empty for that
            # reason (settings.html), and the refusal below keys on it rather
            # than re-deriving `_relation_aliases_context()` here. Re-deriving
            # was the original (broken) shape: measured end-to-end, it let a
            # STALE tab that rendered while the file was unreadable send its
            # empty box after someone repaired the file on disk in the
            # meantime — the re-derived check then saw a readable file, did not
            # refuse, and deleted the just-repaired file. Binding the refusal to
            # what THIS render actually showed, instead of to the file's state
            # at submit time, closes that race in the destructive direction; the
            # genuine-clear path (rendered from a readable file, so no hidden
            # field) is unaffected either way.
            #
            # Truthy, not `== "1"` (#555 gate rev-2): the template only ever
            # sends `"1"`, but a destructive-path guard should not silently
            # disarm on a value that merely drifted from that literal — any
            # non-empty value here means the same thing (this render's box was
            # empty because the file was unreadable). Falsiness, not `is None`:
            # an ABSENT field (`None`, the default) and a PRESENT-but-empty one
            # (`""`, e.g. a forged submit with the field but no value) both take
            # the non-refusal path below, and both are correct there — the
            # template itself only ever omits the field or sends `"1"`, never an
            # empty string, so this is generous rather than load-bearing.
            return _settings(
                request,
                error=(
                    f"Nothing was saved. This box was empty because "
                    f"{RELATION_ALIASES_RELPATH} could not be read when this page "
                    "was loaded, and saving it as submitted would have deleted the "
                    "file instead of fixing it. Paste corrected contents here before "
                    "saving."
                ),
                status_code=400,
            )
        try:
            relation_aliases(text)
        except CorroborationPolicyError as e:
            return _settings(request, error=str(e), status_code=400)
        path = _relation_aliases_path()
        try:
            if text:
                _write_relation_aliases_atomic(path, text + "\n")
            elif path.exists():
                # No truncate-before-fail risk here: `unlink` either removes the
                # whole file or, on failure, leaves it exactly as it was — unlike
                # `write_text`'s open(mode='w'), there is no partial-content state
                # for this branch to land in, so it needs no atomic treatment.
                path.unlink()
        except Exception as exc:  # noqa: BLE001 - same house form as save_prompt_route/reset_prompt_route above
            # #555 BLOCKER-3: previously unguarded, so a write against a file this
            # process cannot write to (e.g. permission-denied) 500'd instead of
            # reporting the failure. Newly reachable now that this page no longer
            # 500s on GET for that same file — before this PR the form was
            # unreachable for a KB in that state.
            #
            # `_write_relation_aliases_atomic` leaves `path` byte-identical to
            # before on any failure (mkstemp + os.replace, never `write_text`'s
            # in-place truncate — #555 gate REV-3), so "Nothing was saved" below
            # is true of a write failure now, not just of the unlink branch.
            #
            # BROAD, matching `save_prompt_route`/`reset_prompt_route`'s stated
            # reasoning rather than the narrower `except OSError` an earlier
            # round of this fix used (#555 gate rev-2, Critic N7): both routes
            # do plain filesystem calls (`mkdir`/`write_text`/`unlink` there,
            # `mkstemp`/`fchmod`/`write`/`fsync`/`os.replace` here) that raise
            # `OSError` in every case anyone has produced, but this file's own
            # precedent (`save_prompt_route`'s comment) declines to trust that
            # as a closed set — "the ways a directory the user can chmod fails
            # are not a set this route can enumerate" — and this route has no
            # narrower a claim to make about its own filesystem calls than that
            # one does about its. One rule, not two.
            #
            # STATUS IS 500, MATCHING `save_prompt_route`/`reset_prompt_route`,
            # NOT 400 (#555 gate rev-7 — an earlier round of this comment kept
            # 400 here and argued the retry consideration was neutral because
            # this route, like those two, is a plain `<form method="post">`
            # with no `hx-post` (checked — `prompts.html` has zero `hx-post`
            # occurrences too). That premise is true but was applied wrong:
            # it is equally true of the routes this one is being compared
            # AGAINST, so it cannot be the reason to diverge from them. It
            # shows only that no CLIENT BEHAVIOUR keys on the split here —
            # not that the argument for 500 does not apply. `save_prompt_route`'s
            # own reasoning ("the user's text was accepted and the disk said
            # no, so repeating the identical request once the mode bits are
            # fixed is exactly the right thing to do — which is what 4xx
            # denies") is a claim about MEANING, not about what a browser
            # does next, and it applies here word for word. `tests/test_web.py`
            # pins 500 for exactly this failure class on the sibling routes,
            # six times: "the write did not happen" (×4), "the delete did not
            # happen" (×2).
            #
            # The decisive reason, independent of any of that: a status code is
            # not only a retry hint, it is a machine-readable claim about WHO
            # was at fault. `relation_aliases(text)` has already succeeded by
            # the time this `try` is entered, so the request itself was valid
            # — the failure below is the server's filesystem, not the client's
            # submission. A 4xx here would be a false attribution of fault, in
            # the one part of the response a machine reads, the same class of
            # defect as a docstring that misstates what the code does. The
            # empty-submit refusal and the parse-error branch above stay at
            # 400 — both are refusals the client can correct by what it sends
            # next (paste valid aliases; paste the file's actual contents
            # rather than the empty box the server itself rendered).
            #
            # The verb names the operation that actually ran, not always
            # "written" (measured, #555 gate rev-2: a directory sitting at the
            # alias path made the `unlink()` branch raise, and "could not be
            # written" named an operation — writing — that never happened).
            action = "written" if text else "removed"
            return _settings(
                request,
                error=f"Nothing was saved. {RELATION_ALIASES_RELPATH} could not be {action}: {exc}",
                status_code=500,
            )
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/root", response_class=HTMLResponse)
    def switch_root(request: Request, root: str = Form(...)):
        path = root.strip()
        if not path:
            return _settings(request, error="KB directory is required", status_code=400)
        try:
            _open_root(Path(path))
        except ConfigCorruptError as e:
            # The target KB's config.json is corrupt. Leave the current KB active
            # rather than switch into one whose provider we cannot trust (#269).
            return _settings(
                request,
                error=f"refused to open KB — its config.json is corrupt: {e}",
                status_code=400,
            )
        except (KBLocationError, OSError, sqlite3.OperationalError) as e:
            return _settings(request, error=f"could not open KB directory: {e}", status_code=400)
        return RedirectResponse("/", status_code=303)

    @app.post("/settings/root/persist", response_class=HTMLResponse)
    def persist_root(
        request: Request,
        root: str = Form(...),
        confirm_persistence: str | None = Form(None),
    ):
        """Persist the current KB only after an explicit, path-bound confirmation."""
        cfg = app.state.cfg
        if cfg is None:
            return _kb_select(request)
        if os.environ.get("VERINOTE_ROOT") is not None:
            return _settings(
                request,
                error="VERINOTE_ROOT controls this process, so its KB cannot be saved as the machine-wide active KB.",
                status_code=400,
            )

        path = root.strip()
        if not path:
            return _settings(request, error="KB directory is required", status_code=400)
        try:
            # Validate the submitted path independently of the active config. This
            # keeps a forged persistence form from recording a worktree descendant.
            assert_kb_root_is_safe_to_create(path)
            target = Path(path).expanduser().resolve()
        except (KBLocationError, OSError) as e:
            return _settings(
                request,
                error=f"could not save KB directory: {e}",
                status_code=400,
            )
        if target != cfg.root:
            return _settings(
                request,
                error="Open this KB before saving it as the machine-wide active KB.",
                status_code=400,
            )
        if confirm_persistence != "on":
            return _settings(
                request,
                error="Confirm the machine-wide KB change before saving it.",
                status_code=400,
            )
        try:
            save_active_root(target)
        except OSError as e:
            return _settings(
                request,
                error=f"could not save KB directory: {e}",
                status_code=400,
            )
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/test", response_class=HTMLResponse)
    def test_connection(request: Request):
        c = app.state.cfg
        if c is None:
            return _kb_select(request)
        if c.provider not in TESTABLE_PROVIDERS:
            return _settings(
                request,
                error="Connection test is not available for this provider.",
                status_code=400,
            )
        try:
            client = get_client(c)
            facts = client.extract_facts(
                source_text="verinote connection test: Ada Lovelace is a mathematician."
            )
        except LLMError as e:
            return _settings(request, error=f"connection failed: {e}", status_code=502)
        return _settings(
            request,
            test_result=f"{client.name} answered with {len(facts)} fact(s) from {c.model}",
        )

    _resume_source_extraction_jobs()
    _resume_repair_jobs()

    return app


# Module-level app for `uvicorn verinote.web.app:app`.
def _default() -> FastAPI:  # pragma: no cover - convenience for uvicorn
    return create_app()
