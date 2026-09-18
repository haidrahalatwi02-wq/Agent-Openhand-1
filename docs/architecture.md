# Architecture

This document explains the design in more depth than the root
[ARCHITECTURE.md](../ARCHITECTURE.md). For the contract each component satisfies,
read the docstring at the top of its module.

## Layering rules

1. **The pipeline never imports a concrete provider, checker or repository.**
   It receives them. `AgentContext` is the only place that chooses concrete
   classes, which is what makes the whole pipeline testable offline.
2. **Components know nothing about each other.** The checker does not know about
   scoring. Scoring does not know about storage. They communicate through the
   `Lead` model and small value objects.
3. **Failures degrade, they do not abort.** Anything that crosses a network
   boundary is allowed to fail, and every such failure is recorded.

## The pipeline

```
SearchQuery
   |
   |  1. search     MultiProviderSearch fans out to providers
   v
SearchResult        raw records grouped per provider + per-provider status
   |
   |  2. normalize  LeadNormalizer maps aliases onto the Lead model
   v
List[Lead]          one lead per raw record
   |
   |  3. deduplicate Deduplicator collapses the same business
   v
List[Lead]
   |
   |  4. check      BaseWebsiteChecker sets website_status and quality
   v
List[Lead]
   |
   |  5. score      LeadScorer sets score, confidence, reasons, priority
   v
List[Lead]
   |
   |  6. store      BaseLeadRepository upserts on dedupe_key
   v
PipelineResult      ranked leads + PipelineStats
```

Each stage is a method on `LeadFinderPipeline` (`search`, `normalize`,
`deduplicate`, `check_website`, `score`, `store`) so a subclass can override one
stage without reimplementing the run.

### Stage detail

**Search.** `MultiProviderSearch` queries every provider and collects
`ProviderResponse` objects. A provider that raises `ProviderError` is captured as
a classified error response (`error_kind`); one that cannot run (missing key,
unreachable service) returns a `skipped_reason`. Either way the other providers
still run. Providers that page through an upstream API report how many calls
they made via `pages_fetched`. The search stage does *not* apply
`SearchQuery.limit`: it sees raw records and cannot tell which are the same
business, so the cap is applied later. Each provider is asked for the full
`limit` and is expected to bound itself internally.

**Normalize.** `LeadNormalizer` applies an alias table, because every source
names things differently (`name`, `business_name`, `title`; `category`, `type`,
`amenity`). It cleans phones, lowercases emails, normalizes URLs, splits social
links out of website fields, and drops known-forbidden personal fields.

Normalization for *display* and normalization for *comparison* are separate.
The normalizer keeps the provider's own wording on the lead (a name stays
`AL Bahr  Restaurant` only if that is what the provider said); comparison
helpers in `utils/text.py` reduce a copy to a comparable form and never write
back.

**Merge and deduplicate.** See [merging multiple providers](#merging-multiple-providers)
below. Ordering is deterministic: survivors keep the position they were first
seen in, and a merge never moves a record. The `limit` is applied *after* this
stage, so duplicates in an earlier provider cannot consume the budget and starve
a later one.

**Website check.** See [docs/website-checker.md](website-checker.md) for the
verification rules, the safety limits on outbound requests, and the error
taxonomy. The checker is registered by name, so an additional strategy can be
added without changing the pipeline.

**Scoring.** Builds a signal dictionary from the lead and the check result, then
evaluates every rule whose conditions all hold. The sum is clamped to
`score_min`..`score_max`. Confidence comes from how many signals were present.
Priority comes from the score plus thresholds, and `hot` additionally requires a
verified website gap — an unverified record is never a prime prospect. Every rule
that fired contributes a human-readable reason. See
[docs/scoring.md](scoring.md) for the rules, thresholds and invariants.

**Storage.** `SQLiteLeadRepository.add` is an upsert on `dedupe_key` that
preserves existing non-empty values when the incoming record is blank. Re-running
a search therefore refreshes and enriches data rather than duplicating or
erasing it.

## Merging multiple providers

When several providers answer, their records are unified into one result set:

```
Google Places ──┐
                ├──> normalize ──> merge ──> de-duplicate ──> results
OpenStreetMap ──┘
```

The layer is provider-agnostic. `search/multi.py` only fans out and aggregates;
`extraction/normalizer.py` cleans each record; `extraction/deduplicator.py`
decides identity; `models/lead.py` owns the merge. A new provider such as
Foursquare or Yelp needs no change to any of them - it only has to return
records with a name, and ideally a `source_id`.

### Identity

`Lead.dedupe_key` is the fast path:

```
if source_id present:  sha1(f"{source}:{source_id}")[:16]
else:                  sha1(name|city|phone_normalized)[:16]
```

Provider ids are namespaced by provider, and ids from different providers are
never compared. A Google place id and an OSM element id are unrelated values
that could collide by accident; treating them as equal would be a silent data
loss.

### Cross-provider matching

The same business is often listed twice with different detail, so a second,
signal-based pass runs. It is deliberately hard to satisfy, because merging two
*different* businesses loses a lead, and that is worse than showing one twice.

1. **Conflicting contact details veto the match.** If both records have a phone
   and the numbers differ, or both have a website on a different domain, they
   are different businesses. This is what separates two same-named branches.
2. **Tier 1 - close names.** With name similarity >= 0.90, any one independent
   agreement confirms: the same phone, the same website domain, the same city,
   or a near-identical address. Without a city on either record, an identical
   name at the same coordinates also confirms.
3. **Tier 2 - loosely similar names** (>= 0.60, e.g. a trading name against a
   legal one). This band is where false positives live, so it additionally
   requires a shared phone *or* website and the same city.
4. **Coordinates are a guard, not a trigger.** Two records further apart than
   `location_conflict_tolerance` (~5.5 km) are kept apart whatever their names
   say - two branches of one chain in one city.

A name match alone is never sufficient. Two businesses called "City Cafe" in
different cities, or "Al Noor Restaurant" and "Al Noor Supermarket", stay
separate. Sharing a website is **not** treated as conclusive on its own:
franchises, chains and shared hosting routinely place unrelated businesses on
one domain, so a shared domain only *corroborates* a name match.

### Field merge

`merge_leads` fills gaps and never overwrites:

- A field that already holds a value is kept; only an empty one is filled.
- A provider's valid value is never replaced by `None`, `""`, `0` or `[]`.
- Lists and maps are unioned (`categories`, `social_links`, `raw`).
- The richer record is chosen as the base, so more information survives.

```
Google:  name="ABC Restaurant"  phone="+967..."  website=missing
OSM:     name="ABC Restaurant"  phone=missing   website="https://example.com"
Result:  name="ABC Restaurant"  phone="+967..."  website="https://example.com"
```

Contradictory values are not blended: the existing value wins, so a merge can
never lose data or invent a value neither provider supplied.

### Source tracking

A merged lead records every provider that found it, so provenance survives the
merge:

| Field | Meaning |
| --- | --- |
| `source` | The primary provider, used for attribution and storage indexes |
| `sources` | Every provider that reported this business |
| `provider_ids` | Provider name → that provider's own stable id |
| `source_urls` | Provider name → that provider's public record URL |

`provider_ids` and `source_urls` are keyed by provider precisely so ids from
different sources never mix.

### Result limits

The limit is applied once, after de-duplication, so the user gets the number of
*distinct* businesses requested. Applying it in the search stage would count
duplicates against the budget: if the first provider returned its limit in
records and two were duplicates, the second provider would be asked for fewer
results than there were slots left, silently costing a unique lead. The search
stage therefore asks every provider for the full limit and the pipeline trims
the merged set.

## Website checker

The checker is deliberately honest. Four statuses:

| Status | Meaning | Evidence required |
| --- | --- | --- |
| `website_exists` | A URL responded successfully | HTTP 2xx/3xx, or a degraded but responding status |
| `website_not_found` | A known URL explicitly does not exist | 404/410 for a URL that came from the data |
| `website_unreachable` | A known URL could not be reached | DNS/timeout/TLS error for a URL from the data |
| `website_unknown` | Inconclusive | No URL, or a guessed domain behaved ambiguously |

The important rule: **a domain guessed from the business name can never produce
`website_not_found`.** A guessed `.com` that returns 404 proves nothing about
whether the business owns a different domain, so it is reported as
`website_unknown`. This is the difference between a usable lead list and one
full of false confidence.

Quality is graded separately from existence:

| Quality | Detected by |
| --- | --- |
| `good` | Substantial content, reachable over HTTPS |
| `weak` | Placeholder/parked page, thin content, stale copyright year, free builder host, degraded HTTP status |
| `social_only` | The "website" is a Facebook/Instagram/WhatsApp link |
| `unknown` | Not established |

Both schemes are resolved without retrying forever: the primary scheme is tried,
then the alternate (`https` -> `http`) so an HTTP-only site is not misreported as
unreachable.

HTTP analysis is regex-based on purpose, to avoid a parser dependency. It
detects the signals that matter (cart, contact page, placeholder text, stale
year) and nothing more. The protocol is `BaseWebsiteChecker`, so a future
`sitemap` or `headless` checker can be added without touching the pipeline.

## Storage schema

One flat `leads` table. Nested fields are JSON text columns.

| Column group | Columns |
| --- | --- |
| Identity | `id`, `dedupe_key` (unique) |
| Business | `business_name`, `business_type`, `categories`, `business_status` |
| Location | `country`, `city`, `address`, `latitude`, `longitude` |
| Contact | `phone`, `email` |
| Web presence | `website_url`, `website_status`, `website_quality`, `website_checked_at`, `social_links` |
| Provenance | `source`, `source_url`, `raw` |
| Scoring | `lead_score`, `score_confidence`, `score_reason`, `score_breakdown`, `priority` |
| Timestamps | `discovered_at`, `last_checked_at` |

Indexes exist on `lead_score`, `city`, `website_status`, `source` and
`discovered_at`, matching the filters and orderings the CLI offers.

`LeadFilter` validates the `order_by` column against a fixed allow-list and uses
bound parameters everywhere, so a value passed to `--order-by` cannot reach SQL.

## Agent Core

Three pieces make multiple agents possible without a rewrite:

- `BaseAgent` — `name`, `description`, `run(**kwargs)`. Three methods.
- `AgentContext` — settings plus lazily resolved repository, providers, checker,
  scorer, normalizer and deduplicator. Agents take a context instead of building
  their own dependencies, which keeps them injectable in tests.
- `AgentManager` — a registry mapping names to agent instances, plus routing.

`LeadFinderAgent` wires the context into `LeadFinderPipeline`. It adds the
reporting helpers the CLI needs (`list_leads`, `top_leads`, `export`, `stats`).

`AgentManager` holds one `AgentContext` and hands it to every agent it registers,
so a search and any later agent share a repository and settings object instead of
each constructing their own. Registration refuses a duplicate name unless
`replace=True`, matching the checker registry: silently swapping an agent would
change behaviour invisibly. `run()` propagates failures, while `run_isolated()`
and `run_all()` convert a failure into a failed `AgentRunResult` so one broken
agent cannot discard the work of the others — the same isolation
`MultiProviderSearch` applies to providers. The manager imports no concrete agent
other than the Lead Finder, and the Lead Finder does not know it exists.

`lead-finder agents` lists the registered set.

## Dependency choices

| Choice | Reason |
| --- | --- |
| `requests` only | Familiar, with a clean fallback in `utils/http.py` |
| `argparse` for the CLI | No dependency; the CLI is thin |
| SQLite | No server, ships with Python, easy to inspect |
| YAML/JSON rule files | Diffable and editable by non-developers |
| Local table renderer | A table was not worth a dependency |

`utils/http.py` routes requests through an injectable transport. That single
seam is what lets every test run offline while the production path still uses
real HTTP.