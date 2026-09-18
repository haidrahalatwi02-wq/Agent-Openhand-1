# Usage

Everything the CLI can do, with worked examples. All examples are safe to run
without any API key.

## Installation

```bash
python -m pip install -e ".[dev]"     # development install with test tools
python -m pip install .               # runtime only
```

Two equivalent entry points:

```bash
lead-finder --help
python -m lead_finder_agent --help
```

## Global options

Available before the sub-command:

| Option | Meaning |
| --- | --- |
| `--db PATH` | Use a specific SQLite database instead of `data/leads.db` |
| `--log-level LEVEL` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `--version` | Print the version |
| `--help` | Help for the command or sub-command |

```bash
lead-finder --db /tmp/scout.db --log-level DEBUG list
```

## `search`

Runs the full pipeline. This is the main command.

```
lead-finder search [--city CITY] [--country COUNTRY] [--type TYPE]
                   [--keywords LIST] [--limit N] [--providers LIST]
                   [--no-website-check] [--no-store] [--max-checks N]
                   [--json] [--show-stats]
```

| Option | Default | Notes |
| --- | --- | --- |
| `--city` | `LEAD_FINDER_DEFAULT_CITY` | e.g. `Aden`, `عدن`, `مدينة عدن` |
| `--country` | `LEAD_FINDER_DEFAULT_COUNTRY` | e.g. `Yemen`, `اليمن` |
| `--type` | none | Business type or a free-text term; Arabic aliases work |
| `--keywords` | none | Comma separated extra terms |
| `--limit` | `50` | Maximum results to request. Must be positive |
| `--providers` | `LEAD_FINDER_PROVIDERS` | Comma separated; `osm`, `sample` |
| `--no-website-check` | off | Skip stage 4 (fast, fully offline) |
| `--no-store` | off | Do not write to the database |
| `--max-checks` | none | Cap website checks in this run |
| `--json` | off | Emit the complete result as JSON |
| `--show-stats` | off | Print per-stage pipeline statistics |

### City and country names

City and country names are matched case- and punctuation-insensitively and
folded onto one canonical spelling, so `عدن`, `مدينة عدن` and `Aden` are the same
search. Country aliases (`اليمن`, `YE`) resolve too. When you give a city but no
country, the country is inferred from the city; an explicit country is always
kept as-is, even when it disagrees with the city.

Anything unrecognized is passed through unchanged rather than guessed at, so a
misspelled city behaves exactly as it always did: the providers simply return no
matches. The known places live in
[`config/data/locations.json`](../lead_finder_agent/config/data/locations.json).

### Examples

Offline demo, no network required:

```bash
lead-finder search --city Aden --type restaurants --providers sample --limit 10
```

The same search written in Arabic — identical results:

```bash
lead-finder search --city عدن --type المطاعم --providers sample --limit 10
```

Live OpenStreetMap search:

```bash
lead-finder search --city Aden --country Yemen --type restaurants --limit 50
lead-finder search --city Sanaa --type "clothing shops" --limit 40
lead-finder search --city Hodeidah --type "car repair" --limit 25
lead-finder search --city Taiz --type electronics --limit 30
```

Preview without touching the database:

```bash
lead-finder search --city Aden --type bakery --no-store --limit 15
```

Machine-readable output for downstream tooling:

```bash
lead-finder search --city Aden --type restaurants --json --limit 20 > results.json
```

With pipeline statistics to see which providers answered:

```bash
lead-finder search --city Aden --type restaurants --show-stats --limit 20
```

### Search output

The table shows the fields that drive a first decision: whether a website
exists, the score, and how much confidence the system has.

```
Business                   | City | Website Status    | Score | Confidence | Priority
---------------------------+------+-------------------+-------+------------+---------
Al Bahr Seafood Restaurant | Aden | website_not_found | 86    | high       | hot
Modern Electronics Aden    | Aden | website_exists    | 64    | high       | warm
```

Messages about skipped providers go to stderr, so `--json` output stays clean:

```
note: provider osm skipped: Overpass returned HTTP 504
```

## `list`

Query stored leads. Defaults to the 25 highest scoring.

```
lead-finder list [--city CITY] [--country COUNTRY] [--type TYPE]
                 [--source NAME] [--priority hot|warm|cold|disqualified]
                 [--website-status STATUS] [--min-score N] [--max-score N]
                 [--limit N] [--order-by FIELD] [--ascending] [--json]
```

```bash
lead-finder list --city Aden --min-score 70
lead-finder list --priority hot --limit 50
lead-finder list --website-status website_not_found
lead-finder list --order-by discovered_at --limit 10
lead-finder list --json > leads.json
```

`--order-by` accepts `lead_score`, `discovered_at`, `last_checked_at`,
`business_name`, `city`, `priority`. Anything else falls back to `lead_score`,
so a mistyped value cannot break the query.

## `show`

Full detail for one lead, including every reason the score was awarded. The id is
the row's `id` column from `list --json` or the `ID` line of `show`.

```bash
lead-finder show 8ba9bc966694b149
```

```
Business        : Al Bahr Seafood Restaurant
Location        : Aden, Yemen
Phone           : +967 71 234 5678
Website         : -
Website status  : website_not_found
Score           : 86 (high confidence, hot)
Reasons:
  - No website found - prime candidate for a new website
  - Business appears to be active
  - Public phone number available for outreach
  ...
```

## `export`

Write stored leads to a file. The format comes from `--format`, or the file
extension when omitted.

```
lead-finder export --output PATH [--format json|csv|tsv]
                   [--city CITY] [--country COUNTRY] [--type TYPE]
                   [--priority PRIORITY] [--website-status STATUS]
                   [--min-score N] [--limit N]
```

```bash
lead-finder export --format csv  --output exports/aden.csv --min-score 60
lead-finder export --format json --output exports/aden.json --city Aden
lead-finder export --output exports/aden.csv --min-score 60     # inferred from .csv
```

CSV output is one row per lead and spreadsheet-safe: embedded commas and quotes
are escaped, and nested values (`social_links`, `score_reason`) are flattened
into `|` separated text.

## `stats`

```bash
lead-finder stats
```

```json
{
  "total": 42,
  "with_website": 7,
  "confirmed_no_website": 23,
  "db_path": "/path/to/data/leads.db"
}
```

Note that `website_unknown` is counted in `total` but not in either website
bucket. Those are leads whose status could not be determined.

## `providers`

```bash
lead-finder providers
```

```
Available search providers:
  osm        OpenStreetMap / Overpass - free, no key required
  sample     Offline fictional sample data - always available, no network
```

Providers that need a missing environment variable are marked `unavailable`
with the reason, instead of failing at search time.

## `agents`

```bash
lead-finder agents
```

```
Registered agents:
  lead_finder        Discover local businesses, check their web presence and score leads
  website_analyzer   Analyse the quality and identity of a lead's website
```

Lists every agent the Agent Manager knows about, so a newly added agent is
visible from the CLI without editing `cli.py`. `--json` gives the same data
machine-readably:

```bash
lead-finder agents --json
```

The manager is how agents are coordinated in code; see
[development.md](development.md#adding-a-new-agent). It starts work by name:

```python
from lead_finder_agent.core import AgentManager

manager = AgentManager().register_default_agents()
manager.run("lead_finder", city="Aden", limit=20)
manager.run("website_analyzer", limit=10)
```

## `analyze`

```bash
lead-finder analyze [--limit 25] [--lead-id ID] [--status STATUS]...
                    [--min-severity LEVEL] [--recheck] [--store] [--json]
```

Runs the **Website Analyzer** over the leads already in the database and reports
what is wrong with each website. It reads the stored website check rather than
re-checking the site, so it makes no network requests unless you pass
`--recheck`.

| Flag | Meaning |
| --- | --- |
| `--limit N` | Analyse at most N stored leads (default 25) |
| `--lead-id ID` | Analyse a single lead |
| `--status STATUS` | Only leads in this state; repeatable (`website_exists`, `website_not_found`, `website_unreachable`, `website_unknown`, `website_not_checked`) |
| `--min-severity LEVEL` | Only leads with a finding at least this severe (`info`, `low`, `medium`, `high`) |
| `--recheck` | Let the checker fill in a missing check — **this uses the network** |
| `--store` | Save the findings onto the stored leads |
| `--json` | Machine-readable output |

```bash
# Everything with something worth acting on
lead-finder analyze --min-severity medium

# Leads whose site the checker confirmed is gone
lead-finder analyze --status website_not_found

# One lead, machine-readable
lead-finder analyze --lead-id 4f2a... --json
```

Output:

```
Business                   | Status              | Quality | Attention | Findings
---------------------------+---------------------+---------+-----------+------------------
Golden Star Bakery         | website_not_checked | unknown | no        | check_unavailable
Aden Fashion House         | website_exists      | weak    | yes       | weak_quality, no_contact_details, no_shop
```

`Attention` is `yes` when a finding is `medium` or `high`. It is `no` for
`check_unavailable` not because the site is fine — the check simply told us
nothing worth acting on — which is why the CLI prints a footer saying so.

The command writes nothing by default; add `--store` to keep the findings. The
findings never change a lead's score. Full detail, including every finding kind
and its severity: [website-analyzer.md](website-analyzer.md).

## A complete workflow

```bash
# 1. Discover restaurants in Aden
lead-finder search --city Aden --country Yemen --type restaurants --limit 50

# 2. Check how it went
lead-finder stats

# 3. Review the best opportunities
lead-finder list --min-score 70 --limit 20

# 4. Inspect one in detail before contacting
lead-finder show <lead-id>

# 5. See what is wrong with their websites
lead-finder analyze --min-severity medium

# 6. Export the shortlist
lead-finder export --format csv --output exports/aden-hot.csv --min-score 70

# 7. Re-run later: existing rows are refreshed, not duplicated
lead-finder search --city Aden --country Yemen --type restaurants --limit 50
```

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success |
| `1` | Runtime error (for example `show` on an unknown id) |
| `2` | Invalid arguments |
| `130` | Interrupted with Ctrl-C |

## Troubleshooting

**`note: provider osm skipped: Overpass returned HTTP 504`**
The public Overpass API is busy. Wait and retry, lower `--limit`, or add
`--providers sample` to keep the rest of the pipeline moving.

**Every search returns `website_unknown`**
Expected for the `sample` provider: it has no real domains to check. Use the
`osm` provider, or run without `--no-website-check` on real data.

**No results for a city**
Check the spelling and add `--country`. Nominatim may resolve a city to a
different administrative area than you expect. Try a broader `--type`.

**The CLI cannot find the database**
Relative paths resolve from the repository root, not the current directory. Use
`--db` with an absolute path if you run from elsewhere.