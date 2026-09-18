# Development

## Setup

```bash
git clone https://github.com/haidrahalatwi02-wq/Agent-Openhand-1.git
cd Agent-Openhand-1
python -m pip install -e ".[dev]"
```

Optional extras:

```bash
python -m pip install -e ".[dev,yaml]"    # PyYAML, for authoring rule files
```

The package needs Python 3.10+. Its only runtime dependency is `requests`.

## Running the tests

```bash
python -m pytest -m "not integration"   # default: fully offline
python -m pytest                        # everything
python -m pytest tests/unit/test_scoring.py -v
python -m pytest --cov=lead_finder_agent --cov-report=term-missing
```

`make test`, `make test-all` and `make clean` wrap the same commands.

### Test layout

| Path | Contents |
| --- | --- |
| `tests/conftest.py` | Fixtures: fake HTTP transport, lead factories, canned HTML and payloads |
| `tests/unit/` | One module per component |
| `tests/integration/` | Whole-system flows across real components |

The Agent Manager's behaviour lives in `tests/unit/test_agent_manager.py`, which
also runs the real Lead Finder through the manager over a real SQLite repository
with only the network faked.

### Testing principles

**No network, ever, in the default run.** The only faked boundary is HTTP. Real
providers, checkers, scorer, normalizer, deduplicator, repository and CLI all
run. `FakeTransport` routes requests to canned responses by URL substring, so a
test states exactly what the outside world returns.

**No mocking our own code.** If a component is hard to test, that is a design
smell to fix, not something to patch around.

**Test behaviour, not implementation.** Assertions target observable outcomes —
a status, a score ordering, a round-tripped field — not private internals.

**Prove the honest cases.** The tests explicitly check that a guessed domain
returning 404 stays `website_unknown`, that a failing provider does not empty the
run, and that thin data yields low confidence. Those are the behaviours most
likely to regress into false confidence.

### Writing a test

```python
def test_lead_without_website_outscores_one_with_a_good_site(
    fake_transport, good_page_html, make_lead
):
    fake_transport.add("great.example", status=200, body=good_page_html)
    checker = HttpWebsiteChecker(client=fake_transport.client(), probe_by_name=False)
    scorer = LeadScorer()

    no_site = make_lead(
        business_name="No Site", website_status=WebsiteStatus.NOT_FOUND,
        business_status=BusinessStatus.ACTIVE, phone="+967 1",
    )
    has_site = make_lead(
        business_name="Has Site", website_url="https://great.example",
        website_status=WebsiteStatus.EXISTS,
    )

    for lead in (no_site, has_site):
        result = checker.check(lead)
        lead.website_status, lead.website_quality = result.status, result.quality

    assert scorer.score(no_site).score > scorer.score(has_site).score
```

## Project conventions

- **Type hints everywhere.** Public functions annotate parameters and returns.
- **Docstrings explain why.** The code says what; docstrings say why a choice was
  made, especially for non-obvious ones.
- **Comments are rare and load-bearing.** No restating the code, no narrating
  changes.
- **Small modules.** Each file has one responsibility and a docstring stating it.
- **Failures are values.** Crossing a network boundary means the call may fail;
  return a status or raise `ProviderSkip`, do not let one failure abort a run.
- **Timestamps are timezone-aware UTC.**

## Changing behaviour

### Scoring

Edit `lead_finder_agent/config/data/scoring_rules.yaml`. No Python changes
needed; the rules are loaded as data. Add a test in
`tests/unit/test_scoring.py` proving the intent, for example that a new rule
changes an ordering or that low confidence still cannot produce a hot lead.

### Website checks

Adjust `config/data/website_signals.yaml` for thresholds and markers. To add more
than tuning — say a sitemap or headless-browser check — implement
`BaseWebsiteChecker` and pass it through `AgentContext.checker`.

### Business types

Add categories, aliases or tags to `config/data/business_types.yaml`. Arabic
aliases belong there too, so they are not hardcoded in provider code.

### Database backend

Implement `BaseLeadRepository` and inject it:

```python
from lead_finder_agent.core.agent import AgentContext, LeadFinderAgent

context = AgentContext(repository=MyPostgresRepository(dsn))
LeadFinderAgent(context=context).run(city="Aden", limit=50)
```

The pipeline and CLI do not change. Keep `dedupe_key` unique in the new backend
and preserve non-empty values on upsert to retain the enrichment behaviour.

## Adding a new agent

This is the intended direction of the project. The seams already exist.

```python
from lead_finder_agent.core.agent import AgentContext, BaseAgent


class WebsiteAnalyzerAgent(BaseAgent):
    """Deeper analysis of websites found by the Lead Finder."""

    name = "website_analyzer"
    description = "Analyse the quality and stack of a lead's website"

    def run(self, limit: int = 20):
        leads = self.context.resolve_repository().find(
            LeadFilter(website_status=WebsiteStatus.EXISTS, limit=limit)
        )
        return [self._analyse(lead) for lead in leads]

    def _analyse(self, lead):
        ...  # reuse context.resolve_checker() for the raw fetch


context = AgentContext()
WebsiteAnalyzerAgent(context).run(limit=10)
```

Then register it with the manager, which coordinates agents by name:

```python
from lead_finder_agent.core import AgentManager

manager = AgentManager().register_default_agents()
manager.register(WebsiteAnalyzerAgent(manager.context))
manager.run("website_analyzer", limit=10)
```

`AgentManager` gives every agent it registers the *same* `AgentContext`, so a
search and a later agent share one repository. The manager is what lets agents
stay independent: they coordinate through the context and the stored data, never
by importing each other.

```python
manager.names()                       # registered agent names, sorted
manager.get("lead_finder")            # the instance
manager.run("lead_finder", city="Aden", limit=20)

# Fan out. A failing agent is reported, not raised, so the others still run.
for outcome in manager.run_all(
    ["lead_finder", "website_analyzer"],
    kwargs_by_agent={"website_analyzer": {"limit": 20}},
    city="Aden",
    limit=20,
):
    if outcome.ok:
        ...
    else:
        print(outcome.agent, "failed:", outcome.error)
```

Agents do not share a signature, so `run_all` takes shared `kwargs` plus an
optional `kwargs_by_agent` mapping to give one agent different arguments.
Without it, fanning a single keyword set across agents with unrelated `run()`
signatures would fail for the wrong reason — the agent would be reported as
broken when the caller simply passed an argument it does not accept.

Two behaviours to rely on:

- `register()` raises on a duplicate name. Pass `replace=True` to swap one
  deliberately — an accidental replacement should not pass unnoticed.
- `run()` propagates an exception; `run_isolated()` and `run_all()` convert it
  into an `AgentRunResult(ok=False, error=...)`. Use the latter for multi-agent
  work, where a partial result beats no result.

`lead-finder agents` lists the registered set, so a new agent is visible from the
CLI without touching `cli.py`.

Rules for new agents:

1. Take an `AgentContext`; never construct your own dependencies.
2. Do one thing. Coordinate through the context and the stored data.
3. Keep network access behind an injectable seam so tests stay offline.
4. Do not import another agent directly.
5. Register against the shared context (`AgentManager(context)`), not a fresh
   one, or the agent will write to a different database than the Lead Finder.

## Adding a provider

See [providers.md](providers.md#adding-a-provider). In short: subclass
`BaseSearchProvider`, implement `search_raw`, register it, add an offline test
using `FakeTransport`.

## Debugging

```bash
lead-finder --log-level DEBUG search --city Aden --type restaurants --limit 5
lead-finder search --city Aden --type restaurants --show-stats --limit 5
lead-finder search --city Aden --type restaurants --no-store --limit 5
```

- `--show-stats` shows which providers answered, how many records each produced,
  how many duplicates were collapsed, and per-stage timings.
- `--no-store` isolates pipeline behaviour from database writes.
- `--no-website-check` removes the slowest stage, so provider issues are obvious.

Common symptoms:

| Symptom | Likely cause |
| --- | --- |
| Every lead is `website_unknown` | The `sample` provider has no real domains |
| A provider is skipped every time | Public service outage or missing key; check `lead-finder providers` |
| Fewer results than `--limit` | The source has no more matches in that area |
| A lead is missing a field it should have | The provider used a key the normalizer does not map; add the alias |

## Releases

1. Update the version in `pyproject.toml` and `lead_finder_agent/__init__.py`.
2. Add an entry to `CHANGELOG.md`.
3. Run the full suite: `python -m pytest`.
4. Commit, tag and push.