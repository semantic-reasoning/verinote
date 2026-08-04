@echo off
rem SPDX-License-Identifier: MPL-2.0
rem
rem Windows launcher: provision the virtual environment if it is missing, then
rem run verinote. Double-clicking this file in Explorer is a supported way to
rem start the app; so is `scripts\run.cmd` from a shell.
rem
rem   scripts\run.cmd                 -> verinote ui   (opens http://127.0.0.1:8731)
rem   scripts\run.cmd ui --port 9000  -> verinote ui --port 9000
rem   scripts\run.cmd status          -> verinote status
rem
rem Arguments are passed through verbatim. Only the no-argument case injects a
rem subcommand, because `--version` and `--help` live on the top-level parser and
rem a launcher that silently rewrote them into `verinote ui --version` would turn
rem the two flags a new user is likeliest to try into an argparse error.
rem
rem Environment overrides (all optional):
rem   PYTHON             interpreter to build the venv with; wins outright
rem   VERINOTE_VENV      venv location            (default: <repo>\.venv)
rem   VERINOTE_EXTRAS    pyproject extras         (default: anthropic,ingest)
rem   VERINOTE_REINSTALL set to 1 to force the install step to run
rem   VERINOTE_DRY_RUN   set to 1 to print the resolution and exit, changing nothing
rem   VERINOTE_NO_PAUSE  set to 1 to never pause on failure (for scripted runs)
rem
rem This script deliberately never sets VERINOTE_ROOT. That variable takes
rem precedence over the active KB saved by the browser picker (verinote/cli.py,
rem `_config_for`), and the web app refuses to save an active KB while it is set
rem — so a root defaulted here would shadow the user's own KB and lock them out
rem of correcting it from the UI. An inherited VERINOTE_ROOT is passed through
rem untouched. The platform default KB root is already outside this working tree
rem (docs/configuration.md), which is where that constraint belongs.

setlocal EnableExtensions

rem Delayed expansion stays OFF so that a `!` in a path is not eaten; every
rem read-after-write below is therefore structured with `call`/`goto` or with
rem `if defined`, which is evaluated at run time rather than at parse time.

rem A version probe must never install a Python behind the user's back: with
rem this set, `py -3.12` can trigger a winget install. `endlocal` reverts it.
set "PYLAUNCHER_ALLOW_INSTALL="

rem `%~dp0` carries a trailing backslash, hence "%~dp0.." and not "%~dp0\..".
for %%I in ("%~dp0..") do set "REPO=%%~fI"

if not defined VERINOTE_VENV set "VERINOTE_VENV=%REPO%\.venv"
set "VENV=%VERINOTE_VENV%"

rem Explorer double-click runs us as `cmd /c "...run.cmd"`, and that window closes
rem the instant we exit — so failures must pause to stay readable. `%cmdcmdline%`
rem is the only signal batch has here, and it cannot tell a double-click from a
rem deliberate `cmd /c run.cmd`: both carry `/c`. The cost of that ambiguity is
rem one spurious line in a scripted run (`pause` returns immediately when stdin
rem is not a console), and VERINOTE_NO_PAUSE=1 suppresses it outright.
rem The quotes must be stripped before `%cmdcmdline%` is expanded into any
rem command: `for %%C in (%cmdcmdline%)` and a bare `if` comparison both let the
rem path's own `(`, `)` and `&` reach the parser, which aborts the script before
rem anything runs. The double-click form is `cmd /c ""C:\...\run.cmd" "`, whose
rem leading `""` closes immediately and leaves the rest bare — so an ordinary
rem `C:\Program Files (x86)\...` or `C:\Repo (1)\...` install killed the launcher
rem in exactly the case this probe exists to serve.
rem
rem This must stay above every `goto :bad_*`: a failure that jumps out before
rem DBLCLICK is set cannot pause, and the window takes the message with it.
set "CCL=%cmdcmdline:"=%"
set "DBLCLICK="
if not "%CCL%"=="%CCL: /c =%" set "DBLCLICK=1"
if "%VERINOTE_NO_PAUSE%"=="1" set "DBLCLICK="

if not defined VERINOTE_EXTRAS set "VERINOTE_EXTRAS=anthropic,ingest"
rem Quotes are stripped first, and that is load-bearing rather than tidiness: a
rem quote hides the character after it from the tests below, so `ingest"&"x`
rem would pass the guard and then be executed — the guard failing *open*, with
rem the run still reporting success. It is also the one character the tests
rem cannot check for, since a `%EXTRAS:"=%` comparison is itself unbalanced.
set "EXTRAS=%VERINOTE_EXTRAS:"=%"
rem EXTRAS is expanded onto `echo` lines (the dry-run report, and the stamp
rem write via WANT), so a metacharacter in it would be parsed as a command. The
rem quiet failure is the one that matters: `&` truncates the stamp, HAVE never
rem again equals WANT, and every launch silently reinstalls. Extras names are
rem PEP 508 identifiers, so refusing anything outside this set costs nothing and
rem is safer than trying to escape it.
rem Each test removes one character and compares: a difference means it was
rem present. Written out literally rather than looped over a character set,
rem because substring replacement with a `for` variable needs delayed expansion,
rem which this script deliberately runs without.
set "BADX="
if not "%EXTRAS%"=="%EXTRAS:&=%" set "BADX=1"
if not "%EXTRAS%"=="%EXTRAS:|=%" set "BADX=1"
if not "%EXTRAS%"=="%EXTRAS:>=%" set "BADX=1"
if not "%EXTRAS%"=="%EXTRAS:<=%" set "BADX=1"
if not "%EXTRAS%"=="%EXTRAS:^=%" set "BADX=1"
if not "%EXTRAS%"=="%EXTRAS:(=%" set "BADX=1"
if not "%EXTRAS%"=="%EXTRAS:)=%" set "BADX=1"
if defined BADX goto :bad_extras
rem `anthropic` matches the built-in default provider (verinote/config.py), so
rem the common path works after one launch rather than two. It is not needed for
rem a legible error: the adapter already turns a missing SDK into
rem `LLMError: anthropic SDK not installed`, naming the remedy. `ingest` makes
rem docx/pdf upload work. The `test` extra from requirements.txt is omitted: a
rem launcher provisions the app, not a dev harness. Vendor-neutral users want
rem VERINOTE_EXTRAS=ingest and an ollama or claudecli provider; neither adapter
rem needs an SDK.

set "STAMP=%VENV%\.verinote-install"
set "VENVPY=%VENV%\Scripts\python.exe"

rem An interrupted `python -m venv` leaves the directory but no interpreter, so
rem the interpreter is the existence test, never the directory.
if exist "%VENVPY%" goto :have_venv

call :resolve_python
if errorlevel 1 goto :no_python
call :check_version
if errorlevel 1 goto :done

if "%VERINOTE_DRY_RUN%"=="1" goto :dry_run

echo [verinote] Creating the virtual environment at "%VENV%".
call "%PYEXE%" %PYARGS% -m venv "%VENV%"
if errorlevel 1 goto :venv_failed
if not exist "%VENVPY%" goto :venv_failed
set "FRESH=1"
goto :install_check

:have_venv
rem Steady state runs no interpreter discovery at all: a venv built with 3.12
rem must never silently rebase onto whatever `py -3` resolves to today.
if "%VERINOTE_DRY_RUN%"=="1" (
    set "PYEXE=%VENVPY%"
    set "PYARGS="
    goto :dry_run
)

:install_check
rem Reinstall only when the declared dependencies or the chosen extras actually
rem changed. Signature is the content hash of pyproject.toml, not its timestamp:
rem two edits inside the same minute share a timestamp, which would claim "fresh"
rem when it is not. certutil is a Windows builtin, so this stays pure batch.
set "PJSIG="
for /f "skip=1 delims=" %%H in ('certutil -hashfile "%REPO%\pyproject.toml" SHA256 2^>nul') do (
    if not defined PJSIG set "PJSIG=%%H"
)
set "PJSIG=%PJSIG: =%"
rem certutil absent or refusing: fall back to the timestamp. Weaker, but it errs
rem toward reinstalling rather than toward claiming a stale venv is current.
if not defined PJSIG for %%I in ("%REPO%\pyproject.toml") do set "PJSIG=%%~tI"

rem The separator must not be a cmd metacharacter: `%WANT%` is expanded on an
rem `echo` line when the stamp is written, so a `|` here would be parsed as a
rem pipe and cmd would try to run the extras list as a command.
set "WANT=%PJSIG%~%EXTRAS%"
set "HAVE="
if exist "%STAMP%" for /f "usebackq delims=" %%S in ("%STAMP%") do if not defined HAVE set "HAVE=%%S"

set "NEEDINSTALL="
set "WHY=Dependencies changed"
if not "%HAVE%"=="%WANT%" set "NEEDINSTALL=1"
rem The console script is what `[project.scripts]` regenerates; its absence is
rem the one way this script notices a `pip uninstall` done behind its back.
if not exist "%VENV%\Scripts\verinote.exe" (
    set "NEEDINSTALL=1"
    set "WHY=The verinote command is missing from the environment"
)
if "%VERINOTE_REINSTALL%"=="1" (
    set "NEEDINSTALL=1"
    set "WHY=VERINOTE_REINSTALL is set"
)

if not defined NEEDINSTALL goto :run

rem Say which of the three reasons actually triggered this, rather than blaming a
rem dependency change for an install the caller forced.
if defined FRESH (
    echo [verinote] First run: installing verinote and its dependencies.
) else (
    echo [verinote] %WHY%: reinstalling verinote.
)
echo [verinote] This downloads packages and can take several minutes.
"%VENVPY%" -m pip install -e "%REPO%[%EXTRAS%]"
if errorlevel 1 goto :pip_failed
> "%STAMP%" echo %WANT%

:run
if "%~1"=="" goto :run_ui
"%VENV%\Scripts\verinote.exe" %*
set "CODE=%ERRORLEVEL%"
goto :done

:run_ui
"%VENV%\Scripts\verinote.exe" ui
set "CODE=%ERRORLEVEL%"
goto :done

rem ---------------------------------------------------------------- subroutines

:resolve_python
rem The interpreter is kept as a program (PYEXE) plus its arguments (PYARGS)
rem rather than one string, so PYEXE can be quoted at every use site. An
rem unquotable single string cannot express both `py -3.12` and
rem `C:\Program Files\Python312\python.exe` — and the latter is a default install
rem location, so collapsing them rejected the interpreter the caller named.
rem An explicit PYTHON wins outright and must not fall through to discovery:
rem silently ignoring it would run a different interpreter than the one asked for.
if defined PYTHON (
    set "PYEXE=%PYTHON%"
    set "PYARGS="
    exit /b 0
)
rem Probe with `--version`, never bare: a bare `py -3.12` opens a REPL and the
rem launcher would sit at `>>>` forever behind a double-clicked window.
rem Newest CI-tested version first; keep in sync with the matrix in
rem .github/workflows/ci.yml. A version the launcher does not have exits 103, and
rem an absent `py` exits 9009 — both non-zero, so the ladder falls through.
for %%V in (3.13 3.12 3.11) do (
    if not defined PYEXE (
        py -%%V --version >nul 2>&1 && call :set_py py -%%V
    )
)
if not defined PYEXE py -3 --version >nul 2>&1 && call :set_py py -3
if not defined PYEXE python --version >nul 2>&1 && call :set_py python
if not defined PYEXE exit /b 1
exit /b 0

:set_py
set "PYEXE=%~1"
set "PYARGS=%~2"
exit /b 0

:check_version
rem This runs inside a `call` frame, so it must report failure with `exit /b`
rem and never `goto` out to a shared exit label: such a jump stays in the call
rem frame, so the exit path would run once on the way out of the subroutine and
rem again in the caller, popping `setlocal` early and pausing twice.
set "PYMAJ="
set "PYMIN="
rem Invoked directly rather than through `for /f ('...')`: that form cannot quote
rem a program whose path contains a space, and every attempt to quote inside it
rem dies on the nested quotes. `call` is what lets a `.cmd` stand-in return here
rem instead of replacing this script's context.
set "VERFILE=%TEMP%\verinote-pyver-%RANDOM%.txt"
call "%PYEXE%" %PYARGS% -c "import sys;print(sys.version_info[0], sys.version_info[1])" > "%VERFILE%" 2>nul
rem No file at all means the redirect failed, not that the interpreter did:
rem blaming a perfectly good `py -3.12` and sending the user off to set PYTHON
rem would be a wrong diagnosis of an unwritable TEMP.
if not exist "%VERFILE%" goto :cv_no_temp
for /f "usebackq tokens=1,2" %%a in ("%VERFILE%") do (
    set "PYMAJ=%%a"
    set "PYMIN=%%b"
)
del "%VERFILE%" >nul 2>&1
if not defined PYMIN goto :cv_not_runnable
if %PYMAJ% LSS 3 goto :cv_too_old
if %PYMAJ% EQU 3 if %PYMIN% LSS 11 goto :cv_too_old
rem Above the tested ceiling we warn and continue rather than refuse:
rem pyproject's `requires-python = ">=3.11"` has no upper bound, and a launcher
rem must not enforce a stricter contract than the package it installs. The real
rem failure mode is a dependency with no wheel for this version falling back to
rem a source build, which the warning makes legible in advance.
if %PYMAJ% EQU 3 if %PYMIN% GTR 13 (
    echo [verinote] Warning: Python %PYMAJ%.%PYMIN% is newer than the versions this
    echo [verinote] project tests ^(3.11-3.13^). If the install fails on a package
    echo [verinote] with no matching wheel, set PYTHON to a 3.12 or 3.13 and retry.
)
exit /b 0

:cv_no_temp
echo [verinote] error: could not write a temporary file under "%TEMP%".
echo [verinote] The interpreter was not the problem. Set TEMP to a writable
echo [verinote] directory and run this script again.
set "CODE=1"
exit /b 1

:cv_not_runnable
echo [verinote] error: "%PYEXE%" could not report its version, so it is not usable.
echo [verinote] Set PYTHON to a working Python 3.11+ interpreter.
set "CODE=1"
exit /b 1

:cv_too_old
echo [verinote] error: Python %PYMAJ%.%PYMIN% is too old; verinote requires 3.11 or newer.
echo [verinote] Set PYTHON to a newer interpreter, e.g.
echo [verinote]     set PYTHON=C:\Python312\python.exe
set "CODE=1"
exit /b 1

rem --------------------------------------------------------------- exit handling

:dry_run
rem Quoted: an unquoted `echo` splits on a `&` in a path and executes the tail.
echo [verinote] dry run - nothing was created or installed.
echo   repo        "%REPO%"
echo   venv        "%VENV%"
echo   interpreter "%PYEXE%" %PYARGS%
echo   extras      "%EXTRAS%"
set "CODE=0"
goto :done

:no_python
echo [verinote] error: no usable Python found.
echo [verinote] Install Python 3.11 or newer from https://www.python.org/downloads/
echo [verinote] or set PYTHON to the interpreter you want, e.g.
echo [verinote]     set PYTHON=C:\Python312\python.exe
set "CODE=1"
goto :done

:bad_extras
echo [verinote] error: VERINOTE_EXTRAS contains characters that are not allowed.
echo [verinote] Use a comma-separated list of extra names, e.g.
echo [verinote]     set VERINOTE_EXTRAS=anthropic,ingest
set "CODE=1"
goto :done

:venv_failed
echo [verinote] error: could not create the virtual environment at "%VENV%".
echo [verinote] Remove that folder and try again, or set VERINOTE_VENV elsewhere.
set "CODE=1"
goto :done

:pip_failed
echo [verinote] error: installing dependencies failed.
echo [verinote] Check the pip output above. To start over, delete
echo [verinote]     "%VENV%"
echo [verinote] and run this script again.
set "CODE=1"
goto :done

:done
rem The app's own exit code reaches this pause too: the commonest repeat failure
rem is a second launch hitting an in-use port or a locked KB, and that message
rem must not vanish with the window.
if defined DBLCLICK if not "%CODE%"=="0" (
    echo.
    pause
)
endlocal & exit /b %CODE%
