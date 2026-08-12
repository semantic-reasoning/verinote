# SPDX-License-Identifier: MPL-2.0
import ast
import inspect
import os
import shlex
import subprocess
import tempfile
import textwrap
from types import SimpleNamespace

import pytest

from verinote.config import Config
from verinote.llm.base import LLMError
from verinote.llm.claude_cli_adapter import ClaudeCliAdapter
from verinote.llm.factory import get_client
from verinote.pipeline.extract import (
    create_chunked_extraction_job,
    process_extraction_job,
)
from verinote.prompts import save_prompt_override
from verinote.store import Store


def _cfg(tmp_path, *, model: str = "", llm_timeout_seconds: float = 600.0) -> Config:
    return Config(
        root=tmp_path,
        db_path=tmp_path / "kb.sqlite",
        provider="claudecli",
        model=model,
        api_key=None,
        base_url=None,
        llm_timeout_seconds=llm_timeout_seconds,
    )


# The two command shapes, as the *caller* reaches them: `extract_facts` goes
# through `_run` (--json-schema), `answer_question` through `_run_text`. Both take
# the text under test as the `-p` argument, which is where an unsendable character
# stops the call. Parametrizing on these is what makes a fix applied to only one
# shape visible -- the shape of #474 itself, whose `except` clauses were copy-pasted
# and drifted.
_INVOCATIONS = [
    pytest.param(lambda a, text: a.extract_facts(source_text=text), id="extract_facts"),
    pytest.param(lambda a, text: a.answer_question(question=text, context="c"), id="answer_question"),
]

_FACT_JSON = (
    '{"facts":[{"subject":"Ada","relation":"is_a",'
    '"object":"mathematician","confidence":0.9}]}'
)


def _no_claude_on_path(tmp_path, monkeypatch) -> None:
    """PATH with no `claude` on it, so a real `subprocess.run` cannot reach one.

    Used by the argument-encoding tests, which must NOT stub `subprocess.run`: a
    stub that raises `ValueError` on demand only proves the `except` clause catches
    what the stub throws. The point is that CPython raises it for real, before any
    process is spawned -- which is also why an absent binary does not matter here.
    """
    empty = tmp_path / "empty-bin"
    empty.mkdir(exist_ok=True)
    monkeypatch.setenv("PATH", str(empty))


def _fake_claude(tmp_path, monkeypatch, reply: bytes) -> None:
    """Put a `claude` on PATH that writes exactly `reply` -- a real process, real pipes.

    PYTHON WRITES THE BYTES; THE SCRIPT ONLY `cat`s THEM. Nothing the fake emits
    passes through a shell's escape handling, because that handling is not
    portable: `printf '\\xff\\xfe'` produces those two bytes under bash and the
    eight ASCII characters `\\xff\\xfe` under dash. Written that way, the
    undecodable-reply tests passed on a macOS `/bin/sh` (bash) and, on a CI runner
    where `/bin/sh` is dash, handed the adapter *valid ASCII* -- so the decode
    clause was never reached and four tests failed for a reason that had nothing to
    do with the code under test. `cat` is POSIX-mandated and interprets nothing.

    PATH is PREPENDED, not replaced: the script needs `cat`, and coming first is
    already enough to shadow a real `claude` on the developer's machine.

    Then the fake is run once and its output checked. A fixture that does not
    produce `reply` must fail saying *that*, in its own words -- the alternative is
    what happened above, a broken fixture arriving as a claim about the adapter.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    payload = tmp_path / "claude-reply.bin"
    payload.write_bytes(reply)
    script = bindir / "claude"
    script.write_text(f"#!/bin/sh\ncat {shlex.quote(str(payload))}\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    # Resolved through PATH, under the environment the adapter will see, so this
    # also proves the shadowing works and not merely that the file was written.
    proof = subprocess.run(["claude"], capture_output=True, check=False)
    assert (proof.returncode, proof.stdout) == (0, reply), (
        f"the FIXTURE is broken, not the adapter: the fake `claude` exited "
        f"{proof.returncode} emitting {proof.stdout!r}, not the {reply!r} this test "
        f"is about ({proof.stderr!r})"
    )


def _unusable_temp_directory(tmp_path, monkeypatch) -> None:
    def boom(**kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(tempfile, "TemporaryDirectory", boom)


def test_factory_selects_claude_cli_adapter(tmp_path):
    assert isinstance(get_client(_cfg(tmp_path)), ClaudeCliAdapter)


def test_claude_cli_extracts_facts_from_stdout(tmp_path, monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=(
                '{"facts":[{"subject":"Ada","relation":"is_a",'
                '"object":"mathematician","confidence":0.9}]}'
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    facts = ClaudeCliAdapter(_cfg(tmp_path)).extract_facts(source_text="Ada is a mathematician.")

    assert facts[0].subject == "Ada"
    assert calls[0][0][0] == "claude"
    assert "--safe-mode" in calls[0][0]
    assert "--no-session-persistence" in calls[0][0]
    assert "--json-schema" in calls[0][0]
    assert "--system-prompt" in calls[0][0]
    assert "-p" in calls[0][0]
    assert calls[0][1]["capture_output"] is True
    assert "cwd" in calls[0][1]
    assert calls[0][1]["stdin"] is subprocess.DEVNULL


def test_claude_cli_uses_model_when_configured(tmp_path, monkeypatch):
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        return SimpleNamespace(
            returncode=0,
            stdout='{"datalog":"answer_q7(V) :- relation(V, \\"is_a\\", \\"person\\")."}',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    line = ClaudeCliAdapter(_cfg(tmp_path, model="sonnet")).translate_query(question="Who?", qid=7)

    assert commands[0][0:3] == ["claude", "--model", "sonnet"]
    assert "--safe-mode" in commands[0]
    assert line.startswith("answer_q7")


def test_claude_cli_normalizes_display_model_names(tmp_path, monkeypatch):
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        return SimpleNamespace(
            returncode=0,
            stdout='{"facts":[]}',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    ClaudeCliAdapter(_cfg(tmp_path, model="Opus 4.8")).extract_facts(source_text="x")

    assert commands[0][0:3] == ["claude", "--model", "opus"]


@pytest.mark.parametrize("invoke", _INVOCATIONS)
def test_claude_cli_missing_binary_is_llm_error(tmp_path, monkeypatch, invoke):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(LLMError, match="claude CLI not found"):
        invoke(ClaudeCliAdapter(_cfg(tmp_path, llm_timeout_seconds=5.0)), "x")


def test_claude_cli_nonzero_is_llm_error(tmp_path, monkeypatch):
    def fake_run(cmd, **kwargs):
        return SimpleNamespace(returncode=2, stdout="", stderr="not logged in")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(LLMError, match="not logged in"):
        ClaudeCliAdapter(_cfg(tmp_path)).extract_facts(source_text="x")


def test_claude_cli_uses_kb_prompt_overrides(tmp_path, monkeypatch):
    commands = []
    save_prompt_override(tmp_path, "extraction", "Custom extraction instructions.")
    save_prompt_override(tmp_path, "claude-json-wrapper", "Schema contract:\n{schema_json}")

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        return SimpleNamespace(returncode=0, stdout='{"facts":[]}', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    ClaudeCliAdapter(_cfg(tmp_path)).extract_facts(source_text="x")

    system = commands[0][commands[0].index("--system-prompt") + 1]
    assert system.startswith("Custom extraction instructions.")
    assert "Schema contract:" in system
    assert '"facts"' in system


# extract_facts drives the schema-constrained `_run` site; answer_question
# drives the separate `_run_text` site. Both hardcoded timeout=180 before, so
# each subprocess call site needs its own guard. 1234.0 differs from both the
# 600.0 default and the old 180 hardcode.
@pytest.mark.parametrize(
    "invoke",
    [
        lambda a: a.extract_facts(source_text="x"),
        lambda a: a.answer_question(question="q", context="c"),
    ],
)
def test_claude_cli_applies_configured_timeout(tmp_path, monkeypatch, invoke):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(returncode=0, stdout='{"facts":[]}', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    invoke(ClaudeCliAdapter(_cfg(tmp_path, llm_timeout_seconds=1234.0)))

    assert calls[0]["timeout"] == 1234.0


def test_claude_cli_prompt_validation_error_is_llm_error(tmp_path):
    path = tmp_path / "policy" / "prompts" / "claude-json-wrapper.md"
    path.parent.mkdir(parents=True)
    path.write_text("Missing required placeholder.\n", encoding="utf-8")

    with pytest.raises(LLMError, match="\\{schema_json\\}"):
        ClaudeCliAdapter(_cfg(tmp_path)).extract_facts(source_text="x")


# --- a pinned model id must survive to the CLI, not collapse to "latest" ---


def _record_commands(monkeypatch):
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        return SimpleNamespace(returncode=0, stdout='{"facts":[]}', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return commands


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-4-8",
        "claude-fable-5",
        "claude-sonnet-5",
        "claude-haiku-4-5",
        "claude-3-opus-20240229",
    ],
)
def test_claude_cli_passes_a_canonical_model_id_through_unchanged(tmp_path, monkeypatch, model):
    """The CLI accepts these verbatim. Rewriting `claude-opus-4-8` to `opus` ran
    whatever is newest instead, so config.json named one model and the CLI ran
    another -- a lost pin with no error to notice it by."""
    commands = _record_commands(monkeypatch)

    ClaudeCliAdapter(_cfg(tmp_path, model=model)).extract_facts(source_text="x")

    assert commands[0][0:3] == ["claude", "--model", model]


def test_claude_cli_keeps_a_pin_that_differs_only_in_capitalisation(tmp_path, monkeypatch):
    """Capitalisation is not a reason to lose a pin: fold it to the canonical id
    rather than letting the alias path claim it."""
    commands = _record_commands(monkeypatch)

    ClaudeCliAdapter(_cfg(tmp_path, model="CLAUDE-OPUS-4-8")).extract_facts(source_text="x")

    assert commands[0][0:3] == ["claude", "--model", "claude-opus-4-8"]


def test_claude_cli_pins_the_id_for_every_call_shape(tmp_path, monkeypatch):
    """`--model` is assembled separately in the JSON-schema and plain-text paths;
    a pin that survived only one of them would still be lost on the other."""
    commands = _record_commands(monkeypatch)
    adapter = ClaudeCliAdapter(_cfg(tmp_path, model="claude-opus-4-8"))

    adapter.extract_facts(source_text="x")  # --json-schema path
    adapter.answer_question(question="Who?", context="c")  # plain-text path

    assert [c[0:3] for c in commands] == [["claude", "--model", "claude-opus-4-8"]] * 2


@pytest.mark.parametrize(
    ("display", "expected"),
    [("Opus 4.8", "opus"), ("Claude Opus", "opus"), ("claude-opus", "opus"), ("SONNET", "sonnet")],
)
def test_claude_cli_still_collapses_loose_display_names(tmp_path, monkeypatch, display, expected):
    """The looseness stays for names that are not ids -- only canonical ids are
    exempt, so pinning is not bought by breaking the display-name convenience."""
    commands = _record_commands(monkeypatch)

    ClaudeCliAdapter(_cfg(tmp_path, model=display)).extract_facts(source_text="x")

    assert commands[0][0:3] == ["claude", "--model", expected]


def test_claude_cli_surfaces_an_unknown_model_id_instead_of_substituting_one(tmp_path, monkeypatch):
    """Passing an id through means a wrong one reaches the CLI, which exits 1 with
    its own message. That loud failure is the point: the alternative was quietly
    running a model the user never chose."""
    def fake_run(cmd, **kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout="There's an issue with the selected model (claude-nonexistent-9).",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(LLMError, match="claude-nonexistent-9"):
        ClaudeCliAdapter(_cfg(tmp_path, model="claude-nonexistent-9")).extract_facts(source_text="x")


# --- one subprocess call site, shared by both command shapes ---


def test_claude_cli_routes_every_command_shape_through_one_call_site(tmp_path, monkeypatch):
    """The bug this refactor precedes (#474) was a missing `except` clause that had
    to be added twice because the body was copy-pasted twice. A shape that skips
    `_invoke` would keep its own copy of the clauses and drift again."""
    seen = []

    def recorder(self, cmd):
        seen.append(cmd)
        return '{"facts":[]}'

    monkeypatch.setattr(ClaudeCliAdapter, "_invoke", recorder)
    adapter = ClaudeCliAdapter(_cfg(tmp_path))

    adapter.extract_facts(source_text="x")  # --json-schema path
    adapter.answer_question(question="Who?", context="c")  # plain-text path

    assert len(seen) == 2
    assert "--json-schema" in seen[0]
    assert "--json-schema" not in seen[1]


# --- text that cannot travel in an exec argument (#474) ---
#
# A chunk carrying a NUL made `subprocess.run` raise `ValueError: embedded null
# byte` *before spawning anything*, and neither call site caught it. The job died
# on the web worker's generic `except Exception` branch as "analysis failed:
# embedded null byte" -- a Python internal, on the wrong branch, for a failure
# that is squarely an LLM-call failure.
#
# None of these stub `subprocess.run`. CPython raising the error for real is the
# whole claim; a stub that raises on demand would only test the stub.


@pytest.mark.parametrize("invoke", _INVOCATIONS)
def test_claude_cli_nul_in_the_text_is_llm_error(tmp_path, monkeypatch, invoke):
    _no_claude_on_path(tmp_path, monkeypatch)

    with pytest.raises(LLMError) as excinfo:
        invoke(ClaudeCliAdapter(_cfg(tmp_path, llm_timeout_seconds=5.0)), "Ada\x00Lovelace")

    message = str(excinfo.value)
    assert "cannot travel in a command-line argument" in message
    # The argument list is encoded before PATH is even consulted, so an absent
    # binary must NOT be what gets reported. Blaming the install for a bad
    # character sends the user to fix something that is not broken.
    assert "claude CLI not found" not in message


@pytest.mark.parametrize("invoke", _INVOCATIONS)
def test_claude_cli_unpaired_surrogate_in_the_text_is_llm_error(tmp_path, monkeypatch, invoke):
    """NUL is not the only unsendable character: `os.fsencode` refuses a surrogate
    it cannot round-trip, raising `UnicodeEncodeError` -- a *different* exception
    from the same call, which the same clause has to cover.

    U+D800 specifically, because it is outside the U+DC80..U+DCFF band that
    surrogateescape round-trips to bytes. `\\udcff` and friends encode fine and
    would make this test pass against a NUL-only fix (see the test below, which
    pins that they keep working).
    """
    _no_claude_on_path(tmp_path, monkeypatch)

    with pytest.raises(LLMError) as excinfo:
        invoke(ClaudeCliAdapter(_cfg(tmp_path, llm_timeout_seconds=5.0)), "Ada\ud800Lovelace")

    message = str(excinfo.value)
    assert "cannot travel in a command-line argument" in message
    assert "claude CLI not found" not in message


@pytest.mark.parametrize("invoke", _INVOCATIONS)
def test_claude_cli_still_sends_a_surrogate_that_round_trips(tmp_path, monkeypatch, invoke):
    """U+DC80..U+DCFF is how surrogateescape carries an undecodable *byte*, and
    `os.fsencode` turns it straight back into that byte. Such text reaches the CLI
    today and must keep reaching it: rejecting it up front -- the tempting
    "validate the string before calling" fix -- would refuse work that succeeds.
    """
    _fake_claude(tmp_path, monkeypatch, _FACT_JSON.encode())

    result = invoke(ClaudeCliAdapter(_cfg(tmp_path, llm_timeout_seconds=5.0)), "Ada\udcffLovelace")

    assert result  # the CLI really ran and its reply came back


@pytest.mark.parametrize("invoke", _INVOCATIONS)
def test_claude_cli_unsendable_text_says_what_to_do_about_it(tmp_path, monkeypatch, invoke):
    """`embedded null byte` names a C-level fact about argv. The user needs the
    thing they can act on: which text is at fault and what to do with it.

    Asserted as literals rather than against the module constant on purpose --
    importing the constant would compare the message to itself and pass for any
    wording at all, including none.
    """
    _no_claude_on_path(tmp_path, monkeypatch)

    with pytest.raises(LLMError) as excinfo:
        invoke(ClaudeCliAdapter(_cfg(tmp_path, llm_timeout_seconds=5.0)), "Ada\x00Lovelace")

    message = str(excinfo.value)
    assert "cannot travel in a command-line argument" in message  # what is wrong
    assert "re-ingest that source" in message  # what to do next
    assert "embedded null byte" in message  # the original cause, still legible


@pytest.mark.parametrize("invoke", _INVOCATIONS)
def test_claude_cli_undecodable_reply_does_not_blame_the_source(tmp_path, monkeypatch, invoke):
    """`text=True` decodes the CLI's stdout AND its stderr as strict UTF-8, so a
    mangled byte on *either* stream surfaces as a `ValueError` (`UnicodeDecodeError`
    is one). Same exception family as the argument clause, opposite direction, and
    the advice cannot be the argument clause's: this says nothing about which stream
    carried the bytes, so "go edit your document" would be a claim the code never
    made. It cannot even promise the answer was lost -- a valid JSON reply on stdout
    is thrown away just the same when one byte of the log beside it will not decode
    -- so the message names the operation that failed and stops there.

    This clause also may not quote its cause, which is why the absence assertions
    below are as load-bearing as the presence one. `UnicodeDecodeError`'s text is
    `'utf-8' codec can't decode byte 0xff in position 0: invalid start byte` -- a
    byte value and an offset read out of the model's reply, which is derived from
    the user's document. Interpolating it into a message that travels to a job row
    and the UI turns an error message into a content oracle. The argument clause
    interpolates *its* cause (pinned by the test above) precisely because the
    character it names cannot be document content; here it can be.
    """
    _fake_claude(tmp_path, monkeypatch, b"\xff\xfe")

    with pytest.raises(LLMError) as excinfo:
        invoke(ClaudeCliAdapter(_cfg(tmp_path, llm_timeout_seconds=5.0)), "Ada Lovelace")

    message = str(excinfo.value)
    assert "was not valid UTF-8" in message
    assert "re-ingest" not in message
    assert "Remove it from the source" not in message
    assert "0xff" not in message  # a byte of the model's output
    assert "position" not in message  # its offset
    assert "codec" not in message  # the decoder's own phrasing, i.e. the raw cause

    # Pinned in full, not by substring: the failure mode this clause has already
    # shown is a sentence drifting into a claim the code never made. Any edit to
    # the wording turns this red and makes someone read it again.
    assert message == (
        "claude CLI request failed: the CLI's output was not valid UTF-8 and could "
        "not be read. Reading the CLI's output is what failed, not sending your "
        "text. If it fails the same way again, check the claude CLI's version."
    )


@pytest.mark.parametrize("invoke", _INVOCATIONS)
def test_claude_cli_timeout_is_llm_error(tmp_path, monkeypatch, invoke):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 5.0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(LLMError, match="timed out"):
        invoke(ClaudeCliAdapter(_cfg(tmp_path, llm_timeout_seconds=5.0)), "x")


@pytest.mark.parametrize("invoke", _INVOCATIONS)
def test_claude_cli_os_error_is_llm_error(tmp_path, monkeypatch, invoke):
    def fake_run(cmd, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(LLMError, match="request failed"):
        invoke(ClaudeCliAdapter(_cfg(tmp_path, llm_timeout_seconds=5.0)), "x")


@pytest.mark.parametrize("invoke", _INVOCATIONS)
def test_claude_cli_unusable_temp_directory_is_llm_error(tmp_path, monkeypatch, invoke):
    """The scratch directory the CLI runs in is part of making the call, so failing
    to create it (a full disk, an unwritable TMPDIR) is a failed LLM call and must
    arrive as one.

    Not hypothetical: merging the two call sites moved the `with` out of the `try`,
    and this `OSError` went from `LLMError` to escaping raw -- straight onto the web
    worker's generic branch as "analysis failed: [Errno 28] ...", the same shape
    #474 was reported as.
    """
    _unusable_temp_directory(tmp_path, monkeypatch)

    with pytest.raises(LLMError) as excinfo:
        invoke(ClaudeCliAdapter(_cfg(tmp_path, llm_timeout_seconds=5.0)), "Ada")

    assert "request failed" in str(excinfo.value)
    assert "No space left on device" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, OSError)


@pytest.mark.parametrize("invoke", _INVOCATIONS)
def test_claude_cli_failing_temp_directory_cleanup_is_llm_error(tmp_path, monkeypatch, invoke):
    """`TemporaryDirectory.__exit__` removes the tree and can fail doing it. The
    call itself already succeeded, but the exception still comes out of this
    function, so it is still this function's job to normalise it -- the same reason
    the creation failure above is caught.
    """
    class _CleanupFails:
        def __enter__(self):
            return str(tmp_path)

        def __exit__(self, *exc_info):
            raise OSError(39, "Directory not empty")

    _fake_claude(tmp_path, monkeypatch, _FACT_JSON.encode())
    monkeypatch.setattr(tempfile, "TemporaryDirectory", lambda **kwargs: _CleanupFails())

    with pytest.raises(LLMError) as excinfo:
        invoke(ClaudeCliAdapter(_cfg(tmp_path, llm_timeout_seconds=5.0)), "Ada")

    assert "request failed" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, OSError)


@pytest.mark.parametrize("invoke", _INVOCATIONS)
def test_claude_cli_only_blames_the_argument_for_the_argument(tmp_path, monkeypatch, invoke):
    """`except ValueError` is only safe because it wraps `subprocess.run` ALONE.

    `ValueError` is a domain type in this repo -- `CorroborationPolicyError`
    subclasses it -- so a clause that says "your text contains a character that
    cannot be sent" would be lying about any other `ValueError` raised anywhere
    inside the guarded region. Keeping that region one statement wide is the whole
    defence, and this pins it: a `ValueError` from a *different* statement (temp
    directory setup) must come out as itself, not relabelled.

    Without this, moving the `with` back inside the shared `try` is invisible.
    """
    def boom(**kwargs):
        raise ValueError("something else entirely")

    monkeypatch.setattr(tempfile, "TemporaryDirectory", boom)

    # Not `LLMError`: it subclasses RuntimeError, so this would not match.
    with pytest.raises(ValueError) as excinfo:
        invoke(ClaudeCliAdapter(_cfg(tmp_path, llm_timeout_seconds=5.0)), "Ada")

    assert "cannot travel in a command-line argument" not in str(excinfo.value)


def _raise(exc):
    def fake_run(cmd, **kwargs):
        raise exc

    return fake_run


def _stub(exc):
    def setup(tmp_path, monkeypatch):
        monkeypatch.setattr(subprocess, "run", _raise(exc))

    return setup


# One entry per `except` clause in `_invoke`, in the order the clauses appear, each
# with text that reaches it: the argument clause needs a NUL, the rest must NOT have
# one or they would trip that clause first. Neither the count nor the order is
# maintained by hand -- the test below reads both off `_invoke`'s source and fails
# when this list stops matching it.
_CAUSES = [
    pytest.param(_unusable_temp_directory, "Ada", OSError, id="temp-directory-creation"),
    pytest.param(_stub(FileNotFoundError()), "Ada", FileNotFoundError, id="missing-binary"),
    pytest.param(
        _stub(subprocess.TimeoutExpired(["claude"], 5.0)),
        "Ada",
        subprocess.TimeoutExpired,
        id="timeout",
    ),
    pytest.param(
        lambda tmp_path, monkeypatch: _fake_claude(tmp_path, monkeypatch, b"\xff\xfe"),
        "Ada",
        UnicodeDecodeError,
        id="undecodable-reply",
    ),
    pytest.param(_no_claude_on_path, "Ada\x00Lovelace", ValueError, id="unsendable-argument"),
    pytest.param(_stub(PermissionError(13, "Permission denied")), "Ada", OSError, id="os-error"),
]


def test_claude_cli_cause_table_mirrors_every_except_clause_in_source():
    """`_CAUSES` claims one entry per `except` clause in `_invoke`. That claim was
    hand-counted once and went stale two commits later, when the temp directory's
    creation clause was added and the table was not. So it is read off the function
    instead of retyped.

    MIRRORS, not covers. This holds the table to the source's clause list and
    nothing more. A clause that no input can reach -- say a `ValueError` subclass
    placed below the `ValueError` clause -- passes here with an entry that never
    fires, and the two `OSError` entries could swap places unnoticed because the
    comparison is by name. Reachability is what the parametrised tests below and
    mutation runs are for.

    `expected_cause` names the clause's *declared* type, not necessarily the type
    its setup raises -- the `os-error` entry raises `PermissionError` into
    `except OSError` -- because it is the clause list this has to line up with.
    """
    source = textwrap.dedent(inspect.getsource(ClaudeCliAdapter._invoke))
    handlers = sorted(
        (node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ExceptHandler)),
        key=lambda node: node.lineno,
    )
    # Not vacuous if `_invoke` is ever refactored so the clauses live elsewhere.
    assert handlers
    # A bare `except:` would be a defect of its own, and would silently drop out
    # of the comparison below rather than failing it.
    assert all(handler.type is not None for handler in handlers)
    clauses = [ast.unparse(handler.type).rsplit(".", 1)[-1] for handler in handlers]

    assert clauses == [param.values[2].__name__ for param in _CAUSES]


@pytest.mark.parametrize("invoke", _INVOCATIONS)
@pytest.mark.parametrize(("setup", "text", "expected_cause"), _CAUSES)
def test_claude_cli_keeps_the_original_exception_as_the_cause(
    tmp_path, monkeypatch, invoke, setup, text, expected_cause
):
    """Every clause chains with `from exc`. The `LLMError` text is written for a
    human, which means it drops detail; a traceback with no `__cause__` leaves
    nowhere to recover that detail when the human message turns out to be wrong
    about what happened.
    """
    setup(tmp_path, monkeypatch)

    with pytest.raises(LLMError) as excinfo:
        invoke(ClaudeCliAdapter(_cfg(tmp_path, llm_timeout_seconds=5.0)), text)

    assert isinstance(excinfo.value.__cause__, expected_cause)


@pytest.mark.parametrize("invoke", _INVOCATIONS)
def test_claude_cli_does_not_swallow_a_programming_error(tmp_path, monkeypatch, invoke):
    """A misconfigured timeout makes `subprocess.run` raise `TypeError` from inside
    the guarded call. Catching `ValueError` there is only defensible because it
    stays that narrow: widened to `Exception`, this bug would be reported to the
    user as "your text contains a bad character" and the real defect -- a
    non-numeric setting -- would never be seen.
    """
    _fake_claude(tmp_path, monkeypatch, _FACT_JSON.encode())

    with pytest.raises(TypeError):
        invoke(ClaudeCliAdapter(_cfg(tmp_path, llm_timeout_seconds="not-a-number")), "Ada")


# --- one unsendable chunk must not discard the rest of the job (#474) --------


def test_one_unsendable_chunk_fails_alone_and_the_job_keeps_going(tmp_path, monkeypatch):
    """The whole point of normalising to `LLMError`, exercised end to end.

    `process_extraction_job` catches `LLMError` per chunk, marks that chunk failed
    and moves on; anything else escapes the loop and takes the job with it. So the
    NUL goes in chunk *zero* deliberately -- ORDER IS WHAT MAKES THIS TEST MEAN
    ANYTHING. With it in the last chunk, the earlier chunks would already have been
    committed before the crash and every assertion below would pass against the
    unfixed adapter. In chunk zero, the unfixed adapter kills the loop on the first
    iteration: chunk one is never claimed, no candidate is ever written, and
    `process_extraction_job` raises instead of returning.

    A real `claude` on PATH, not a stub, for the same reason as the tests above:
    the `ValueError` has to come from CPython refusing to build the argv.
    """
    store = Store(tmp_path / "kb.sqlite")
    store.init_schema()
    source_id = store.add_source("doc.txt")
    # Two paragraphs, each under the chunk size, over it together -> two chunks.
    # No overlap, or chunk zero's NUL would be copied into chunk one's prefix and
    # both chunks would fail. Nothing here matches `_ROLE_CUE_RE`: a match would
    # make the pipeline issue a second, focused extraction call per chunk.
    text = (
        "Ada Lovelace was a mathematician.\x00 She wrote notes on the engine.\n\n"
        "Grace Hopper was a computer scientist. She built early compilers."
    )
    job_id = create_chunked_extraction_job(
        store,
        source_id=source_id,
        source_text=text,
        provider="claudecli",
        model="",
        chunk_chars=120,
        chunk_overlap_chars=0,
    )
    assert [c["chunk_index"] for c in store.source_chunks(job_id)] == [0, 1]
    assert "\x00" in store.source_chunks(job_id)[0]["text"]
    assert "\x00" not in store.source_chunks(job_id)[1]["text"]

    _fake_claude(tmp_path, monkeypatch, _FACT_JSON.encode())

    # Returns; before the fix the ValueError escaped this call entirely.
    process_extraction_job(store, ClaudeCliAdapter(_cfg(tmp_path, llm_timeout_seconds=5.0)), job_id=job_id)

    job = store.get_extraction_job(job_id)
    assert job["status"] == "failed"
    # Written by `_refresh_extraction_job` (`store/db.py:3336`), NOT by the web
    # worker: with the fix in place the `LLMError` never leaves
    # `process_extraction_job` (`extract.py:459-461` swallows it per chunk), so
    # neither of the worker's branches runs at all. Before the fix the `ValueError`
    # tore through that loop and the worker's generic `except Exception` wrote
    # "analysis failed: embedded null byte" -- the symptom #474 was reported as.
    assert str(job["message"]).startswith("Analysis failed: 1 chunk(s) failed, 1/2 complete")

    chunks = store.source_chunks(job_id)
    assert chunks[0]["status"] == "failed"
    assert "cannot travel in a command-line argument" in str(chunks[0]["error"])
    assert chunks[1]["status"] == "done"  # `pending` before the fix: never reached
    # The job's candidate tally can only have come from chunk one -- chunk zero
    # never reached the LLM.
    assert int(job["candidate_count"]) > 0
    assert len(store.facts()) >= 1  # zero before the fix
    store.close()
