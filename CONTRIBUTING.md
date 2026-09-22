# Contributing

Thanks for considering a contribution. This document describes how to set up,
what the project expects from changes, and how to get a change merged.

## Getting started

```bash
git clone https://github.com/haidrahalatwi02-wq/Agent-Openhand-1.git
cd Agent-Openhand-1
python -m pip install -e ".[dev]"
python -m pytest -m "not integration"      # must pass before you start
```

Python 3.10+.

## Before you open a pull request

1. Run the offline suite: `python -m pytest -m "not integration"`.
2. Run the CLI against the sample provider to confirm nothing is broken:
   `lead-finder search --city Aden --type restaurants --providers sample --no-website-check`
3. Update documentation if behaviour, flags or configuration changed.
4. Add a `CHANGELOG.md` entry under `Unreleased`.
5. Keep the change focused. One concern per pull request.

## What the project values

**Honesty about data.** Never report a fact the data does not support. If a check
is inconclusive, say so. `website_unknown` exists for this reason and should stay
distinct from "no website".

**Simplicity.** This is an MVP, not a framework. Prefer the standard library over
a dependency, a small module over a large one, and the obvious solution over the
clever one. New dependencies need a clear justification.

**Testability.** New behaviour comes with tests. Tests run offline: fake the
network boundary, never our own components.

**Clear failure.** Anything crossing a network boundary may fail. Failures become
statuses or `ProviderSkip`, never a crash that takes down a whole run.

**Respectful data collection.** Only public business data from sources that
permit it. No personal data, no scraping behind logins, no aggressive request
rates against community services.

## Style

- Follow the surrounding code. Match its structure and naming.
- Type-hint public functions and methods.
- Write docstrings that explain intent and trade-offs, not restatements of code.
- Keep comments rare and meaningful.
- No unused code, no commented-out blocks, no debug prints in committed code.

## Tests

- Place unit tests in `tests/unit/`, integration tests in
  `tests/integration/`.
- Name tests for the behaviour they assert, not the method they call.
- Use `FakeTransport`, `make_lead` and the other fixtures in `conftest.py`.
- Cover the edge case, not just the happy path: empty input, a failing provider,
  a malformed record, an inconclusive check.
- Mark network-dependent tests with `@pytest.mark.integration`; they are excluded
  by default.

## Adding a provider

Subclass `BaseSearchProvider`, implement `search_raw`, register it, and add an
offline test. Full walkthrough in [docs/providers.md](docs/providers.md#adding-a-provider).

## Adding an agent

Subclass `BaseAgent`, take an `AgentContext`, do one thing well. Full walkthrough
in [docs/development.md](docs/development.md#adding-a-new-agent).

## Reporting a bug

Include what you ran, what you expected, what happened, and the relevant
`--log-level DEBUG` output. If a provider was involved, name it and include the
skipped-reason message.

## Reporting a data problem

If a lead contains data that should not be stored, that is a priority. Open an
issue describing the field and the source. Do not paste personal data into the
issue; describe it instead.

## Commits

- Present tense, imperative mood: "Add SerpAPI provider", not "Added".
- One logical change per commit.
- Reference an issue number when one exists.

## License

By contributing you agree your work is licensed under the MIT license, as in
[LICENSE](LICENSE).