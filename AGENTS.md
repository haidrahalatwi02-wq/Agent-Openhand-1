# AGENTS.md

Repository-specific guidance for AI agents working in this project.

## Project

**Lead Finder Agent** — finds local businesses that have a real commercial
presence but no proper website, then scores and exports them as sales leads.

- Language: Python 3.10+ (`from __future__ import annotations` throughout).
- Runtime dependency: `requests` only. Do not add dependencies without need.
- Package: `lead_finder_agent/`. Tests: `tests/`. Docs: `docs/`.

## Commands

```bash
python -m pip install -e ".[dev]"     # install for development
python -m pytest -m "not integration" # offline suite (default, no network)
python -m pytest                      # all tests, including integration
python -m pyflakes lead_finder_agent tests examples
make test / make run-example / make clean
```

The CLI is reachable as `lead-finder` or `python -m lead_finder_agent`.

## Architecture — do not break these seams

- The pipeline (`core/pipeline.py`) depends only on interfaces, never on
  concrete providers, checkers or repositories. Concrete classes are chosen in
  `core/agent.py::AgentContext`. Keep it that way.
- Network access goes through `utils/http.py`, which accepts an injectable
  transport. That single seam is what keeps the test suite offline.
- Failures that cross a network boundary must degrade, not abort a run:
  return a status, set a `skipped_reason`, or raise `ProviderSkip`.
- Rule data (scoring, business types, website signals) lives in
  `config/data/*.yaml` and must stay editable without code changes.
- Only the Lead Finder agent exists. `BaseAgent`/`AgentContext` are the seam for
  future agents — do not build them speculatively.

## Core invariant — honesty about data

`website_unknown` means "inconclusive" and must never be collapsed into
"no website". A domain guessed from a business name can never produce
`website_not_found`. Low-confidence leads can never be presented as hot.
There are tests for all three; keep them passing.

## Data policy

Collect public business data only. Never add fields for personal names,
government identifiers, payment data, or anything not published by the
business. The normalizer enforces a denylist in `extraction/normalizer.py`.

## Testing conventions

- Tests must run offline. Fake the network boundary only (use `FakeTransport`
  from `tests/conftest.py`); never mock internal components.
- Mark network-dependent tests with `@pytest.mark.integration`.
- Unit tests in `tests/unit/`, integration tests in `tests/integration/`.
- Name tests for the behaviour asserted, not the method called.
- Current status: 633 tests passing (612 offline unit, 21 integration).

## Style

- Type-hint public functions and methods.
- Docstrings explain intent and trade-offs, not what the code plainly says.
- Comments only for non-obvious invariants; do not narrate changes.
- Timestamps are timezone-aware UTC.

## Git and GitHub

- Never commit `.env`, `data/*.db` or `exports/*` (except the `.gitkeep` files).
- Never write real API keys into committed files; use environment variables.
- The default branch is `main`. Do not push directly to it.

### Known environment limitation

The `GITHUB_TOKEN` in this workspace authenticates as `hidrhalatywm773-web`,
which has **read-only** access (`push: false`) to
`haidrahalatwi02-wq/Agent-Openhand-1`. Pushing and forking both return 403 /
"Resource not accessible by integration".

Work must be committed locally and the limitation reported to the user. A token
with write access to the repository is required to push.
