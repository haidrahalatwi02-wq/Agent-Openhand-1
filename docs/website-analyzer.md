# Website Analyzer Agent

The Website Analyzer is the second agent in the project. It reads the leads the
Lead Finder already stored and turns each stored website check into a short,
evidence-backed list of findings.

It answers a different question from the checker. The checker asks *"does this
business have a website?"* and returns one status. The analyzer asks *"what is
wrong with the website this business has?"* and returns findings an outreach
message can actually use — a missing contact page, no HTTPS, a profile on a
social network instead of a site the business owns.

```
Lead Finder Agent  ->  stored leads  ->  Website Analyzer Agent  ->  findings
   (discovery)          (repository)          (analysis)
```

## What it refuses to do

Three rules shape the whole agent, and each exists because the opposite is easy
and wrong.

### 1. It never re-derives a website status

The status comes from the stored `WebsiteCheckResult` the checker wrote into the
lead's `raw["website_check"]`. The analyzer does not re-run a check to form its
own opinion.

Re-running would let the analyzer contradict the lead it is describing — the
stored lead says `website_exists` while the analysis says otherwise — and it
would put network access on what is otherwise a read-only reporting path. The
checker is only used when a caller explicitly passes `recheck=True` for a lead
that has no usable stored check.

### 2. It inherits the honesty invariant

`website_unknown` and `website_not_checked` mean *we could not tell*. The
analyzer reports that as `check_unavailable` — a gap in our knowledge:

> No website was confirmed. That means the check was inconclusive, not that the
> business lacks a website.

It is worded that way on purpose, and there is a test asserting the wording. A
reader who sees "no website confirmed" in a report must not walk away believing
the business has no site.

Only `website_not_found` — a completed check that established the page is gone —
produces `no_website_confirmed`. This is the same distinction that keeps such a
lead out of the `hot` priority in scoring.

Because an inconclusive check is not a problem *with the business*, it is also
`info` severity, and does not set `needs_attention`.

### 3. Absent data is not a negative finding

A stored check may not carry every detail. An older record, a hand-written
payload, or a check written before a field existed can all omit keys.

The dataclass defaults are `False` and `None`, which are indistinguishable from a
recorded negative. So the analyzer consults the payload itself: a finding about
HTTPS, a contact page, a shop, response time, truncation or the title is only
emitted when the payload actually recorded that field. A record that omits
`has_https` produces no claim about HTTPS at all.

Without this, an old record would generate confident findings about details
nobody ever checked.

## Findings

| Kind | Severity | Emitted when |
| --- | --- | --- |
| `no_website_confirmed` | high | The check completed and the page does not exist (`website_not_found`) |
| `social_only_presence` | high | The only web presence is a social media profile |
| `site_unreachable` | medium | A URL exists but the request failed |
| `no_https` | medium | A reachable site is served over plain HTTP |
| `weak_quality` | medium | A reachable site is thin, outdated, or on a free builder |
| `identity_uncertain` | medium | The page could not be tied confidently to the business |
| `no_contact_details` | medium | A reachable site has no contact details or contact page |
| `no_shop` | low | A reachable site has no e-commerce or ordering markers |
| `slow_response` | low | Response time exceeded `slow_response_ms` |
| `truncated_response` | low | The page was cut off at the configured size limit |
| `missing_title` | low | The page has no usable title |
| `check_unavailable` | info | The check was inconclusive, or the source URL was unusable |

Findings describe a real site. A social profile is not one, so the
site-quality findings (`no_shop`, `no_contact_details`, and the others) are not
reported against it — a profile was never meant to have a shop, and saying it
lacks one is noise about the wrong thing.

### Severities

Severities are labels, not scores. The analyzer deliberately does not feed the
lead score: re-scoring a stored lead from an analysis pass would make a stored
score depend on whether, and when, the analyzer last ran. Findings are
descriptive detail.

`needs_attention` is true when any finding is `medium` or `high`. A lead whose
check was inconclusive is neither "fine" nor flagged — read its findings.

## Configuration

Thresholds and severities live in
[`website_analysis.yaml`](../lead_finder_agent/config/data/website_analysis.yaml)
(`.json` alongside it, used when PyYAML is unavailable). Editing it needs no code
change.

```yaml
version: 1
thresholds:
  slow_response_ms: 3000     # above this, a reachable site is reported as slow
  min_title_length: 3        # a shorter title counts as missing
severities:
  social_only_presence: high
  no_https: medium
  no_shop: low
  check_unavailable: info
```

A caller can override either section in code; the override merges one level deep,
so replacing `thresholds` leaves `severities` alone:

```python
from lead_finder_agent.agents import WebsiteAnalyzerAgent

agent = WebsiteAnalyzerAgent(config={"thresholds": {"slow_response_ms": 1500}})
```

## Using it

### From Python

```python
from lead_finder_agent.agents import WebsiteAnalyzerAgent
from lead_finder_agent.core import AgentContext

agent = WebsiteAnalyzerAgent(AgentContext())
for analysis in agent.run(limit=20, min_severity="medium"):
    print(analysis.summary())
```

`run()` accepts:

| Argument | Meaning |
| --- | --- |
| `limit` | Bound how many stored leads are read |
| `lead_id` | Analyse one lead by id |
| `statuses` | Restrict to these `WebsiteStatus` values |
| `recheck` | Let the checker fill in a missing check (uses the network) |
| `min_severity` | Keep only leads with a finding at least this severe |

The agent is **read-only by default**. It annotates the analyses it returns and
writes nothing back unless constructed with `store=True`.

### Through the Agent Manager

```python
from lead_finder_agent.core import AgentManager

manager = AgentManager().register_default_agents()
manager.run("lead_finder", city="Aden", limit=20)
manager.run("website_analyzer", limit=10)
```

Both agents are registered by `register_default_agents()` and share one
`AgentContext`, so the analyzer reads the repository the Lead Finder wrote to.

### From the CLI

```bash
lead-finder analyze --limit 25
lead-finder analyze --status website_not_found --min-severity medium
lead-finder analyze --json
lead-finder analyze --store          # save findings onto the leads
lead-finder analyze --recheck        # fills in missing checks (network)
```

## Reading an analysis

```
Business                   | Status              | Quality | Attention | Findings
---------------------------+---------------------+---------+-----------+------------------
Golden Star Bakery         | website_not_checked | unknown | no        | check_unavailable
Aden Fashion House         | website_exists      | weak    | yes       | weak_quality, no_contact_details, no_shop
```

`needs_attention` is `no` for the first row not because the site is fine — we do
not know that it has one — but because the check told us nothing worth acting
on. The CLI prints a footer saying so, so the column cannot be misread.

## Testing

Covered by `tests/unit/test_website_analyzer.py`, organised around the
invariants rather than the implementation:

- `TestHonestyAboutInconclusiveChecks` — an inconclusive check never becomes a
  confirmed absence, and is reported as our own gap
- `TestAbsentDataIsNotANegative` — an omitted detail never becomes a finding
- `TestFindingsFromTheStoredCheck` — each finding, and the severity it carries
- `TestAnalysisWithoutAStoredCheck` — the fallback to the lead's own fields
- `TestRechecking` — the checker is untouched unless `recheck=True`
- `TestRunningOverStoredLeads` — selection, limits, filtering, and that nothing
  is written unless `store=True`
- `TestEndToEndThroughTheManager` — both agents over one shared context

`tests/integration/test_website_analyzer_integration.py` runs the analyzer over
payloads the real `HttpWebsiteChecker` produced. The unit tests hand-write that
payload, so they would keep passing if the checker changed what it records; the
integration tests close that gap.

## Related documentation

- [docs/website-checker.md](website-checker.md) — how a status is decided, and
  why a failed check is not "no website"
- [docs/scoring.md](scoring.md) — the same honesty invariant, applied to scores
- [docs/architecture.md](architecture.md) — where agents sit in the system
- [docs/development.md](development.md) — adding a further agent
- [docs/data-model.md](data-model.md) — the stored `website_check` payload
