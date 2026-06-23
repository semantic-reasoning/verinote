# verinote

[![ci](https://github.com/semantic-reasoning/verinote/actions/workflows/ci.yml/badge.svg)](https://github.com/semantic-reasoning/verinote/actions/workflows/ci.yml)

**Honest knowledge base.** An LLM extracts source-backed *candidate* facts from your
documents; the deterministic **wirelog** logic engine verifies them; you keep a human
review gate before any fact is promoted to engine input. Runs as a local web app.

> Borrows the *concept* of [factlog](https://github.com/semantic-reasoning/factlog)
> (neurosymbolic: LLM extracts, a Datalog/wirelog engine verifies) but is a
> from-scratch implementation — no shared code.

## Why

A free-text wiki drifts: facts go stale, contradict each other, and lose their
sources. verinote keeps the knowledge base *honest* by pairing a neural extractor
with a symbolic verifier:

- **LLM extracts** source-backed candidate facts (provider-agnostic — see below).
- **wirelog verifies** consistency deterministically. Because every fact is
  re-checked by the engine, swapping to a cheaper or local model never compromises
  correctness.
- **You review.** Facts move `candidate → needs_review → confirmed/accepted`
  through a human gate; `superseded` retires them.

## Design (locked)

| Concern        | Decision |
|----------------|----------|
| Logic engine   | **wirelog** (`pyrewire`). Confirmed rows compile to `.dl`; the engine checks them. |
| LLM            | **Hand-rolled `LLMClient` adapters** (Anthropic / OpenAI / Ollama). No vendor lock-in. |
| Web            | **FastAPI + HTMX + Jinja** — server-rendered partials, no JS build step. |
| Storage        | **SQLite** is the system-of-record (OLTP). **DuckDB** attaches it read-only for analytics. `.dl` is derived from confirmed rows. |

## Status

Early scaffold (`v0.0.1`). Working today: SQLite store + the review-queue toggle
loop in the web UI. Stubbed: LLM extraction, wirelog compile/check wiring.

### v1 vertical slice

1. Upload a text source → DB
2. Auto-extract candidates via one LLM adapter
3. **Review queue UI** (toggle / accept / reject)
4. Compile confirmed → wirelog `.dl` → run check → show report
5. Dashboard

Deferred: NL→Datalog query, gated self-correction (repair), coverage critic,
multi-provider expansion.

## Quickstart

```bash
pip install -e ".[anthropic,analytics,test]"   # pick the providers you use
verinote init      # scaffold a local KB (SQLite) under ./data
verinote ui        # launch the web app at http://localhost:8731
```

## License

Mozilla Public License 2.0 — see [LICENSE](LICENSE). MPL-2.0 is a file-level
copyleft: modifications to verinote's own source files stay open, while it can
still be combined with proprietary code in separate files.
