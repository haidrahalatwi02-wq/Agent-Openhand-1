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
`ProviderResponse` objects. A provider that raises is captured as an error
response; one that cannot run (missing key, unreachable service) returns a
`skipped_reason`. Either way the other providers still run. The result is capped
at `SearchQuery.limit`.

**Normalize.** `LeadNormalizer` applies an alias table, because every source
names things differently (`name`, `business_name`, `title`; `category`, `type`,
`amenity`). It cleans phones, lowercases emails, normalizes URLs, splits social
links out of website fields, and drops known-forbidden personal fields.

**Deduplicate.** Two mechanisms:

- Exact: `Lead.dedupe_key`, derived from the source id when present, otherwise
  from normalized name + city + phone. This is the key storage upserts on.
- Fuzzy: leads are bucketed by the first alphanumerics of their name, then
  compared with `difflib.SequenceMatcher` inside the same bucket. Same city and
  a high ratio merges them; an identical phone merges regardless of name.

Merging keeps the richest value for each field, so a later, more complete record
enriches the earlier one instead of blanking it.

**Website check.** See [the checker module](#website-checker) below.

**Scoring.** Builds a signal dictionary from the lead and the check result, then
evaluates every rule whose conditions all hold. The sum is clamped to
`score_min`..`score_max`. Confidence comes from how many signals were present.
Priority comes from the score plus thresholds. Every rule that fired contributes
a human-readable reason.

**Storage.** `SQLiteLeadRepository.add` is an upsert on `dedupe_key` that
preserves existing non-empty values when the incoming record is blank. Re-running
a search therefore refreshes and enriches data rather than duplicating or
erasing it.

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

Two pieces make future agents possible without a rewrite:

- `BaseAgent` — `name`, `description`, `run(**kwargs)`. Three methods.
- `AgentContext` — settings plus lazily resolved repository, providers, checker,
  scorer, normalizer and deduplicator. Agents take a context instead of building
  their own dependencies, which keeps them injectable in tests.

`LeadFinderAgent` wires the context into `LeadFinderPipeline`. It adds the
reporting helpers the CLI needs (`list_leads`, `top_leads`, `export`, `stats`).
An `AgentManager` would be a registry mapping names to agent instances plus a way
to route work between them; it is intentionally not built yet.

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