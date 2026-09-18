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
- Rule data (scoring, business types, website signals, website analysis) lives
  in `config/data/*.yaml` and must stay editable without code changes.
- Two agents are implemented: the Lead Finder (`core/agent.py`) and the Website
  Analyzer (`agents/website_analyzer.py`). `BaseAgent`/`AgentContext` are the
  seam for further agents and `core/manager.py::AgentManager` is the registry
  that coordinates them. Add agents by subclassing `BaseAgent` and registering
  them in `register_default_agents()` (lazy import, to avoid cycles); do not
  build the remaining roadmap agents speculatively.
- An agent must not import another agent. They coordinate through the shared
  `AgentContext` and the stored data.
- A reporting agent reads what earlier stages recorded and must not re-derive
  it: the analyzer reports the stored website check rather than re-checking, so
  it can never contradict the lead it describes. Agents are read-only unless the
  caller explicitly opts in (`WebsiteAnalyzerAgent(store=True)`).

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
- Current status: 764 tests passing (730 offline unit, 34 integration).

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

Earlier sessions found the workspace `GITHUB_TOKEN` authenticated as a different
account with **read-only** access to `haidrahalatwi02-wq/Agent-Openhand-1`
(`push: false`), so pushing and forking returned 403 / "Resource not accessible
by integration".

The token has since been updated. It now authenticates as `haidrahalatwi02-wq`,
which owns the repository and has `push: true`. Check before relying on this
either way:

```bash
curl -s -H "Authorization: Bearer $GITHUB_TOKEN" \
  https://api.github.com/repos/haidrahalatwi02-wq/Agent-Openhand-1 \
  | python -c "import sys,json; print(json.load(sys.stdin)['permissions'])"
```

If `push` is `false`, work must be committed locally and the limitation reported;
a token with write access is required to push.
