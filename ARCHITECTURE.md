# Architecture

## Goals

1. Ship one working agent, not a framework.
2. Make every stage replaceable, so the next agent does not force a rewrite.
3. Never assert something the data does not support.
4. Run with zero configuration and no paid services.

## Layers

```
                       +---------------------------+
   CLI / library  ---> |        Agent Core         |
                       |  BaseAgent, AgentContext  |
                       +-------------+-------------+
                                     |
                       +-------------v-------------+
                       |      LeadFinderPipeline   |
                       | search - normalize -      |
                       | dedupe - check - score -  |
                       | store - results           |
                       +--+--------+--------+------+
                          |        |        |
              +-----------+  +-----+----+  +--------+-----------+
              | Search    |  | Checker  |  | Scoring | Storage  |
              | providers |  | HTTP     |  | rules   | SQLite   |
              +-----------+  +----------+  +---------+----------+
                    |                             |
              +-----v------+               +------v------+
              |  Providers |               |  Exporters  |
              | OSM, sample|               | JSON / CSV  |
              +------------+               +-------------+
                          \        /
                        +--v------v--+
                        | Extraction |
                        | normalize  |
                        | dedupe     |
                        +------------+
```

The dependency direction is always inward: the pipeline depends on interfaces
(`BaseSearchProvider`, `BaseWebsiteChecker`, `BaseLeadRepository`), never on
concrete implementations. Concrete classes are chosen in `AgentContext`.

## Components

| Component | Module | Responsibility |
| --- | --- | --- |
| Agent Core | `core/agent.py` | `BaseAgent` contract, `AgentContext` dependency bundle, `LeadFinderAgent` |
| Pipeline | `core/pipeline.py` | Runs the seven stages, records statistics, isolates failures |
| Search | `search/` | Provider interface, registry, multi-provider fan-out, OSM and sample providers |
| Extraction | `extraction/` | Field mapping/normalization and de-duplication |
| Checker | `checker/` | Website existence, reachability and quality |
| Scoring | `scoring/` | Declarative rule engine producing score, confidence, reasons |
| Storage | `storage/` | Repository interface, SQLite backend, JSON/CSV exporters |
| Config | `config/` | Settings from environment, packaged rule data, file loading |
| Models | `models/` | `Lead`, `SearchQuery`, enums and normalization helpers |
| CLI | `cli.py`, `cli_output.py` | Argument parsing, table rendering, JSON output |

## Data flow

```
SearchQuery
   -> providers.search_raw()      raw dicts, source-specific
   -> LeadNormalizer              Lead objects, consistent fields
   -> Deduplicator                one Lead per real business
   -> BaseWebsiteChecker          website_status + website_quality
   -> LeadScorer                  lead_score + confidence + reasons + priority
   -> BaseLeadRepository          stored, de-duplicated on dedupe_key
   -> PipelineResult              ranked leads + statistics
```

## Key design decisions

**Failures are data, not exceptions.** A provider that times out is skipped and
recorded in `PipelineStats.providers`; the run continues with the other sources.
A single lead whose website check throws is logged and skipped. Only programming
errors propagate.

**Inconclusive is a first-class outcome.** `website_unknown` exists so the
system never reports "no website" from a failed DNS lookup or a guessed domain.
The scoring engine treats unknown as weak evidence, not as proof.

**Deduplication uses two mechanisms.** A stable `dedupe_key` (derived from the
source id, else name + city + phone) makes storage-level upserts exact. A fuzzy
pass collapses near-identical names within the same city. Explicit phone matches
merge regardless of name.

**Rules are data.** Scoring lives in `config/data/scoring_rules.yaml`. Changing
sales priorities should not require touching Python. The same applies to business
type aliases and website signals.

**Storage is behind an interface.** `BaseLeadRepository` is small (add, find,
count, delete, clear). Swapping SQLite for Postgres means writing one class; the
JSON columns map directly onto relational columns or JSONB.

**Confidence is separate from score.** A score says how good the opportunity
looks; confidence says how much we actually know. `priority` derives from both,
so a high score on thin data is never presented as a hot lead without a caveat.

## Adding a second agent

The Lead Finder is agent #1. The extension points already exist:

```python
from lead_finder_agent.core.agent import AgentContext, BaseAgent


class OutreachAgent(BaseAgent):
    name = "outreach"
    description = "Draft outreach messages for scored leads"

    def run(self, limit: int = 10):
        repository = self.context.resolve_repository()
        leads = repository.top(limit)
        return [self._draft(lead) for lead in leads]


context = AgentContext()
result = OutreachAgent(context).run(limit=5)
```

An `AgentManager` that registers agents by name and routes between them is a
small addition on top of this; nothing in `LeadFinderPipeline` or the CLI needs
to change. The intended end state is:

```
Lead Finder Agent -> Agent Manager -> { Lead Finder, Website Analyzer,
                                        Outreach, Follow-up, CRM, Reporting }
```

See [development.md](development.md) for the full walkthrough.

## Testing strategy

- Unit tests per component, all offline: fake HTTP transports, the `sample`
  provider, in-memory SQLite.
- Integration tests wire real components together, still offline.
- No mocks of our own code. Only the network boundary is faked, because that is
  the only thing that genuinely cannot be relied upon in CI.

## Known trade-offs

- **SQLite** is right for single-user CLI use. Concurrent writers will need a
  server database.
- **HTML analysis is regex-based** to avoid a parser dependency. It detects
  carts, contact pages, placeholders and stale copyright years; it is not a
  general-purpose scraper.
- **Business-type aliases are curated**, not generated. Coverage is good for
  common categories and falls back to a broad `shop=*` query otherwise.
- **No enrichment of email addresses.** Emails are only stored when a public
  source already publishes them.