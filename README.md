# Lead Finder Agent

Find local businesses that have a real commercial presence but **no proper
website**, then score them as sales leads for web-design and e-commerce work.

Give it a city and a business type, and it will search public sources, clean and
de-duplicate what it finds, check whether each business has a working website,
score the opportunity, store the result and export it.

```bash
lead-finder search --city Aden --type restaurants --limit 50
```

```
Business                   | City | Website Status      | Score | Confidence | Priority
---------------------------+------+---------------------+-------+------------+---------
Al Bahr Seafood Restaurant | Aden | website_not_found   | 86    | high       | hot
Golden Star Bakery         | Aden | website_not_found   | 86    | high       | hot
Siraji Beauty Salon        | Aden | website_unknown     | 86    | high       | hot
Aden Fashion House         | Aden | website_not_found   | 78    | medium     | hot
Modern Electronics Aden    | Aden | website_exists      | 64    | high       | warm
```

> **Data policy.** This project only uses public business data from sources that
> permit it, and it never collects personal, private or sensitive data. See
> [docs/data-model.md](docs/data-model.md) for exactly what is stored and why.

---

## Why it exists

Most local businesses in emerging markets run entirely on Facebook, Instagram or
WhatsApp. They are real businesses with customers and revenue, but they have no
website — which makes them the best possible prospects for someone selling
website or online-store services.

Finding them by hand does not scale. Lead Finder Agent automates the boring part:
discovery, verification and prioritisation. What it deliberately does *not* do is
claim more than it can prove — if a website check is inconclusive, the lead is
marked `website_unknown` rather than pretending there is no website.

---

## Quick start

### 1. Install

```bash
git clone https://github.com/haidrahalatwi02-wq/Agent-Openhand-1.git
cd Agent-Openhand-1
python -m pip install -e ".[dev]"
```

Python 3.10 or newer. The only runtime dependency is `requests`.

### 2. Configure (optional)

The project runs with **zero configuration**. To customise it:

```bash
cp .env.example .env
```

Everything in `.env.example` is optional. Nothing requires a paid API key.

### 3. Run the tests

```bash
python -m pytest -m "not integration"   # offline, no network needed
```

### 4. Run your first search

```bash
# Offline demo using bundled sample data - good for a first look
lead-finder search --city Aden --type restaurants --providers sample --limit 10

# Live search using OpenStreetMap (free, no key required)
lead-finder search --city Aden --country Yemen --type restaurants --limit 50

# Real business data via Google Places (needs GOOGLE_PLACES_API_KEY)
export GOOGLE_PLACES_API_KEY="your-key-here"
lead-finder search --city Aden --country Yemen --type restaurants --limit 20 --providers google_places

# Any country and city works - nothing is hard-coded to one market
lead-finder search --city Lisbon --country Portugal --type bakeries --limit 20 --providers google_places
```

### 5. Review the results

```bash
lead-finder list --min-score 70
lead-finder show <lead-id>
lead-finder stats
```

### 6. Export for outreach

```bash
lead-finder export --format csv  --output exports/aden-restaurants.csv --min-score 60
lead-finder export --format json --output exports/aden-restaurants.json
```

---

## Commands

| Command | Purpose |
| --- | --- |
| `search` | Search for businesses, check websites, score and store them |
| `list` | List stored leads with filters (`--city`, `--min-score`, `--priority`, ...) |
| `show` | Full detail for one lead, including every score reason |
| `export` | Export stored leads to JSON, CSV or TSV |
| `stats` | Database summary |
| `providers` | List search providers and whether they are available |
| `agents` | List the agents registered with the Agent Manager |
| `analyze` | Analyse the websites of stored leads (Website Analyzer agent) |

Useful `search` flags:

```bash
--city / --country / --type / --keywords   # what to look for
--limit 50                                 # how many results
--providers osm,sample                     # which sources to query
--no-website-check                         # skip the website stage (fast, offline)
--no-store                                 # preview without writing to the database
--max-checks 20                            # cap website checks in one run
--json / --show-stats                      # machine-readable output, pipeline stats
```

Useful `analyze` flags:

```bash
--limit 25                                 # how many stored leads to analyse
--lead-id <id>                             # analyse one lead
--status website_not_found                 # only leads in this state (repeatable)
--min-severity medium                      # only leads worth acting on
--recheck                                  # check leads that have none (uses the network)
--store                                    # save the findings onto the leads
--json                                     # machine-readable output
```

Full reference: [docs/usage.md](docs/usage.md).

---

## How it works

```
Input (city, type, limit)
   |
   v
Search         Query every configured provider; one failing source never stops the run
   |
   v
Normalize      Map source-specific fields onto one consistent Lead model
   |
   v
Deduplicate    Collapse the same business found in several sources
   |
   v
Website Check  Decide: exists / not found / unreachable / unknown (+ quality)
   |
   v
Lead Scoring   Score + confidence + human-readable reasons, from configurable rules
   |
   v
Storage        Upsert into SQLite, de-duplicated by a stable key
   |
   v
Results        Ranked leads, printed and exportable
```

Every stage is a separate, independently testable component with an interface you
can replace. Details: [ARCHITECTURE.md](ARCHITECTURE.md) and
[docs/architecture.md](docs/architecture.md).

---

## Project layout

```
lead_finder_agent/
├── search/       Search providers (Google Places, OpenStreetMap, sample) + registry
├── checker/      Website existence and quality checks
├── scoring/      Configurable rule engine
├── extraction/   Normalization and de-duplication
├── storage/      Repository interface, SQLite backend, exporters
├── core/         Agent Core: pipeline, agent contracts, Agent Manager
├── agents/       Agents beyond the Lead Finder (Website Analyzer)
├── config/       Settings, env handling and rule data
├── models/       Lead, SearchQuery and related data models
└── cli.py        Command line interface
tests/            Unit and integration tests (offline by default)
docs/             Usage, configuration, architecture, data model, providers, website checker, scoring, website analyzer, development
```

---

## Scoring

Scoring answers one question: *is this a good prospect for a website or online
store?* Rules live in
[`scoring_rules.yaml`](lead_finder_agent/config/data/scoring_rules.yaml) and are
data, not code — edit the file or point `LEAD_FINDER_SCORING_RULES` at your own
copy.

Signals that raise the score:

| Signal | Points |
| --- | --- |
| No website found | +35 |
| No website information available | +20 |
| Social-only presence (Facebook/Instagram/WhatsApp) | +18 |
| Weak website (placeholder, thin, outdated) | +22 |
| Business is active | +15 |
| Public phone available | +12 |
| Good rating / many reviews | +8 / +7 |
| Email, address, description, categories, social links | +4 … +8 |

Signals that lower it:

| Signal | Points |
| --- | --- |
| Business permanently closed | −50 (disqualified) |
| Good, working website | −45 |
| Too little data to judge | −10 |

Every lead also carries a **confidence** level (`low` / `medium` / `high`) based
on how many signals were present, so a high score built on thin data is never
presented as a certainty. `priority` (`hot` / `warm` / `cold` / `disqualified`)
is derived from the score and thresholds.

---

## Extending it

The architecture is built for more agents, and the coordination layer for them
now exists. The Lead Finder is the first agent; the **Agent Manager** registers
agents by name and routes work between them.

```
Lead Finder Agent        <-- implemented
      |
Agent Manager            <-- implemented: register, run, isolate failures
      ├── Lead Finder      <-- done
      ├── Website Analyzer <-- done
      ├── Outreach Agent
      ├── Follow-up Agent
      ├── CRM Agent
      └── Reporting Agent
```

Adding an agent means subclassing `BaseAgent` and registering it with the
manager; nothing in the pipeline changes.

```python
from lead_finder_agent.core import AgentManager

manager = AgentManager().register_default_agents()
manager.run("lead_finder", city="Aden", limit=20)
manager.run("website_analyzer", limit=10)
```

`lead-finder agents` lists what is registered. Agents registered with one
manager share a single `AgentContext`, so they read and write the same
repository rather than each building their own. `manager.run_all()` runs several
agents with **per-agent isolation** — one failing agent is reported as a failed
`AgentRunResult` instead of discarding the work the others did.

The **Website Analyzer** is the second agent, and shows the pattern: it reads
the leads the Lead Finder stored, turns each stored website check into findings,
and never re-derives a status the checker already recorded. See
[docs/website-analyzer.md](docs/website-analyzer.md).

Adding a data source means subclassing `BaseSearchProvider` and registering it.
Both walkthroughs are in
[docs/development.md](docs/development.md).

---

## Documentation

| Document | Contents |
| --- | --- |
| [docs/usage.md](docs/usage.md) | Every command, flag and worked example |
| [docs/configuration.md](docs/configuration.md) | Environment variables, rule files, tuning |
| [docs/architecture.md](docs/architecture.md) | Components, data flow, extension points |
| [docs/data-model.md](docs/data-model.md) | Every Lead field, and the data policy |
| [docs/providers.md](docs/providers.md) | Built-in providers and how to add one |
| [docs/website-checker.md](docs/website-checker.md) | How a website is verified, and why a failed check is not "no website" |
| [docs/scoring.md](docs/scoring.md) | How leads are scored, and why `hot` needs a verified gap |
| [docs/website-analyzer.md](docs/website-analyzer.md) | The Website Analyzer agent: findings, severities, and what it refuses to claim |
| [docs/development.md](docs/development.md) | Setup, testing, adding agents and providers |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Architecture overview |
| [CONTRIBUTING.md](CONTRIBUTING.md) | How to contribute |
| [CHANGELOG.md](CHANGELOG.md) | Release history |

---

## Requirements and limitations

- Python 3.10+. Only `requests` is required at runtime.
- The **OpenStreetMap** provider needs internet access. Overpass and Nominatim
  are free community services: use a descriptive `LEAD_FINDER_USER_AGENT`,
  searches are rate-limited on purpose, and heavy use is discouraged.
- The **sample** provider is offline fictional data for demos and tests.
- The **Google Places** provider returns real business data and needs a paid
  `GOOGLE_PLACES_API_KEY`. Without a key it is skipped automatically, so it is
  safe to list it among your providers. Its results are subject to Google's
  terms of service, and coverage is uneven outside dense urban areas. See
  [docs/providers.md](docs/providers.md) for the full list of limitations.
- Searches are not tied to any country. Country, city, type, keywords and limit
  all come from the caller.
- `website_unknown` is a real, expected outcome. It means the check was
  inconclusive — not that the business has no website.
- Statuses come from public tags and HTTP responses at a point in time. Verify
  before contacting a business.

## License

MIT — see [LICENSE](LICENSE).