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

Then, when you want them coordinated, add a manager:

```python
class AgentManager:
    def __init__(self, context):
        self.context = context
        self._agents = {}

    def register(self, agent):
        self._agents[agent.name] = agent
        return agent

    def get(self, name):
        return self._agents[name]


manager = AgentManager(AgentContext())
manager.register(LeadFinderAgent(manager.context))
manager.register(WebsiteAnalyzerAgent(manager.context))
manager.get("lead_finder").run(city="Aden", limit=20)
```

Rules for new agents:

1. Take an `AgentContext`; never construct your own dependencies.
2. Do one thing. Coordinate through the context and the stored data.
3. Keep network access behind an injectable seam so tests stay offline.
4. Do not import another agent directly.

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