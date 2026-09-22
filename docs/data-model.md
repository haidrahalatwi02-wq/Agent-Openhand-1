# Data model

This document lists every field a lead can hold, where each value comes from, and
the rules this project follows about data it will not collect.

## Data policy

The project collects **public business information only**. The rules below are
enforced in code, not just documented.

**Collected** (all from public sources):

- Business name, type and public description
- Public business phone number
- Public business email, only when a source already publishes it
- Street address and public coordinates
- Public business social-media page URLs
- Website URL and the result of checking it
- Public rating and review counts
- The source the record came from and its URL

**Never collected:**

- Personal names of owners or employees
- National ID numbers, passports, or any government identifier
- Payment or financial data
- Private contact details that are not published by the business
- Scraped content from behind a login

The normalizer strips a denylist of such keys (`national_id`, `passport`,
`credit_card`, and similar) from incoming records before they are stored, so a
source cannot smuggle personal data in through a raw payload. See
`extraction/normalizer.py`.

## The `Lead` model

| Field | Type | Description |
| --- | --- | --- |
| `id` | str | Stable identifier; defaults to `dedupe_key` |
| `dedupe_key` | str | Unique key used to prevent duplicates (see below) |
| `business_name` | str | Public business name |
| `business_type` | str \| None | Normalized category, e.g. `restaurant` |
| `country` | str \| None | Country |
| `city` | str \| None | City |
| `address` | str \| None | Public street address |
| `phone` | str \| None | Public business phone, cleaned to digits and `+` |
| `email` | str \| None | Public business email, lowercased; only if already public |
| `source` | str \| None | Provider that supplied the record, e.g. `osm` |
| `source_url` | str \| None | Link back to the original public record |
| `website_url` | str \| None | Website, normalized; social links are moved to `social_links` |
| `website_status` | enum | `website_exists`, `website_not_found`, `website_unreachable`, `website_unknown`, `website_not_checked` |
| `website_quality` | enum | `good`, `weak`, `social_only`, `unknown` |
| `website_checked_at` | datetime \| None | When the website was last checked |
| `social_links` | dict | Platform → URL, e.g. `{"facebook": "https://..."}` |
| `description` | str \| None | Public description, truncated to 500 characters |
| `latitude` / `longitude` | float \| None | Public coordinates |
| `business_status` | enum | `active`, `closed`, `unknown` |
| `review_count` | int \| None | Public review count |
| `rating` | float \| None | Public rating |
| `categories` | list[str] | Additional public categories |
| `raw` | dict | The source payload, cleaned and JSON-safe, minus forbidden keys |
| `lead_score` | int | Computed opportunity score |
| `score_confidence` | enum | `low`, `medium`, `high` |
| `score_reason` | list[str] | Human-readable reasons for the score |
| `score_breakdown` | dict | Rule id → points contributed |
| `priority` | enum | `hot`, `warm`, `cold`, `disqualified` |
| `discovered_at` | datetime | When the lead was first seen |
| `last_checked_at` | datetime \| None | When the lead was last refreshed |

All timestamps are timezone-aware UTC.

## Deduplication key

`dedupe_key` decides whether two records are the same business.

```
if source_id present:  sha1(f"{source}:{source_id}")[:16]
else:                  sha1(name|city|phone_normalized)[:16]
```

- `name` is lowercased with punctuation collapsed.
- `phone` keeps digits only, so `+967 71 234-5678` and `967712345678` match.
- Exact repetitions collapse at the storage layer through a unique index on
  `dedupe_key`.
- Near-identical names in the same city are collapsed by the fuzzy pass in
  `Deduplicator`, not by the key.

When two records merge, each field keeps its richest available value. An empty
value never overwrites a populated one.

## Status semantics

### `website_status`

| Value | Meaning | What it does to the score |
| --- | --- | --- |
| `website_exists` | A URL responded successfully | Depends on quality; a good site lowers priority |
| `website_not_found` | A known URL returned 404/410 | Strong positive signal |
| `website_unreachable` | A known URL failed to respond | Positive, but weaker evidence |
| `website_unknown` | Inconclusive | Weak positive only, never treated as proof |
| `website_not_checked` | The check stage was skipped | No signal |

`website_unknown` is not an error state. It is the honest answer when a check
cannot reach a conclusion, and the scoring engine weights it accordingly.

### `priority`

| Value | Default rule |
| --- | --- |
| `hot` | Score ≥ 70 and confidence meets the minimum |
| `warm` | Score ≥ 45 |
| `cold` | Score ≥ 20 |
| `disqualified` | Business is permanently closed |

Thresholds are configurable; see
[configuration.md](configuration.md#thresholds).

## Provenance

Every lead keeps `source`, `source_url` and the cleaned `raw` payload. This lets
anyone trace a record back to the public source it came from, which matters both
for correcting mistakes and for respecting the terms of each data source.

## Exports

CSV and JSON exports contain the same fields as the model. Nested values
(`social_links`, `score_reason`) are flattened into `|` separated text in CSV so
each lead stays on one row.

```json
{
  "id": "8ba9bc966694b149",
  "business_name": "Al Bahr Seafood Restaurant",
  "city": "Aden",
  "website_url": null,
  "website_status": "website_not_found",
  "lead_score": 86,
  "score_confidence": "high",
  "priority": "hot",
  "score_reason": [
    "No website found - prime candidate for a new website",
    "Business appears to be active",
    "Public phone number available for outreach"
  ]
}
```

## Retention

Leads live in a local SQLite file the operator controls
(`LEAD_FINDER_DB_PATH`). Deleting that file removes everything. There is no
telemetry, no upload, and no external storage.