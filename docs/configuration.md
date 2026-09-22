# Configuration

The project works with no configuration at all. Everything here is optional and
every setting has a working default.

## Where configuration lives

| Kind | Location | Committed? |
| --- | --- | --- |
| Environment variables | `.env` (copy from `.env.example`) | No — `.env` is git-ignored |
| Scoring rules | `lead_finder_agent/config/data/scoring_rules.yaml` | Yes |
| Business-type aliases | `lead_finder_agent/config/data/business_types.yaml` | Yes |
| Website check signals | `lead_finder_agent/config/data/website_signals.yaml` | Yes |

Secrets belong in environment variables, never in files that are committed.
`.env.example` contains placeholders only.

## Loading order

Settings are resolved once, in this order (later wins):

1. Built-in defaults in `Settings`.
2. `.env` in the repository root, if present, loaded by `python-dotenv` when
   installed.
3. Real environment variables.
4. Explicit overrides, such as the CLI `--db` / `--log-level` flags.

## Environment variables

### General

| Variable | Default | Meaning |
| --- | --- | --- |
| `LEAD_FINDER_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `LEAD_FINDER_DB_PATH` | `data/leads.db` | SQLite file. Relative paths resolve from the repo root |
| `LEAD_FINDER_DEFAULT_COUNTRY` | `Yemen` | Used when a search omits `--country` |
| `LEAD_FINDER_DEFAULT_CITY` | `Aden` | Used when a search omits `--city` |
| `LEAD_FINDER_PROVIDERS` | `osm,sample` | Providers to query, in order |

### HTTP

| Variable | Default | Meaning |
| --- | --- | --- |
| `LEAD_FINDER_USER_AGENT` | `LeadFinderAgent/0.1 (+repo url)` | Sent on every request. OpenStreetMap requires an identifying agent |
| `LEAD_FINDER_HTTP_TIMEOUT` | `12` | Seconds, for provider and website-check requests |
| `LEAD_FINDER_MAX_RETRIES` | `0` | Retries for transient provider failures |
| `LEAD_FINDER_OVERPASS_URL` | `https://overpass-api.de/api/interpreter` | Overpass endpoint |
| `LEAD_FINDER_NOMINATIM_URL` | `https://nominatim.openstreetmap.org` | Geocoding endpoint |

### Scoring and rules

| Variable | Default | Meaning |
| --- | --- | --- |
| `LEAD_FINDER_SCORING_RULES` | packaged data | Path to a custom scoring rules file (`.yaml`, `.json`) |
| `LEAD_FINDER_BUSINESS_TYPES` | packaged data | Path to a custom business-type alias file (`.yaml`, `.json`) |

If the path does not exist the packaged defaults are used and a warning is
logged, so a typo cannot break a run.

### Optional provider keys

These are only read by a provider that asks for them. None is required.

| Variable | Used by |
| --- | --- |
| `GOOGLE_PLACES_API_KEY` | A Google Places provider, if you add one |
| `SERPAPI_API_KEY` | A SerpAPI provider, if you add one |
| `LEAD_FINDER_CUSTOM_API_KEY` | Placeholder for a custom provider |

A provider that declares `requires_key_env` is automatically reported as
unavailable when the variable is missing, instead of failing mid-search:

```python
class MyProvider(BaseSearchProvider):
    name = "my-provider"
    requires_key_env = "MY_PROVIDER_API_KEY"
```

## Customising scoring

Scoring rules are data. To change sales priorities without touching code, point
`LEAD_FINDER_SCORING_RULES` at your own file:

```yaml
# my-rules.yaml
name: "Agency priorities"
thresholds:
  hot_score: 65        # be more generous about what counts as hot
  warm_score: 40

# Only these rules are overridden; every other default rule still applies.
rules:
  - id: no_website
    points: 50         # this agency cares most about missing websites
    description: "No website - our ideal prospect"
    when:
      website_missing: true

  - id: has_email
    points: 20
    description: "Direct email available"
    when:
      has_email: true
```

```bash
export LEAD_FINDER_SCORING_RULES=/path/to/my-rules.yaml
```

Set `replace: true` at the top of the file to discard the defaults entirely and
use only your rules:

```yaml
replace: true
rules:
  - id: only_rule
    points: 10
    when:
      has_phone: true
```

### Rule format

Each rule needs `id` and `points`. `when` maps a signal name to an expected
value, and the rule fires only when *all* conditions hold.

- Booleans match exactly: `{"website_missing": true}`.
- Numbers are thresholds meaning "at least": `{"rating": 4.0}` fires at 4.0 or
  above and never fires when the value is missing.
- Duplicate rule ids are rejected at load time.

### Available signals

| Signal | True when |
| --- | --- |
| `website_missing` | The check confirmed there is no website |
| `website_unknown` | The check was inconclusive |
| `website_null` | No website URL is recorded at all |
| `has_website` / `website_is_good` / `website_is_weak` | From the website check |
| `website_social_only` | The only web presence is social media |
| `has_phone` / `has_email` / `has_address` | Field is present |
| `has_social` / `has_description` / `has_categories` | Field is present |
| `business_active` / `business_closed` | Recorded business status |
| `has_good_rating` / `has_many_reviews` | Past thresholds in `thresholds` |
| `data_quality_high` / `data_quality_low` | Based on `data_quality_min_fields` |

### Thresholds

| Key | Default | Meaning |
| --- | --- | --- |
| `good_rating` | `4.0` | Minimum rating for `has_good_rating` |
| `reviews_present` | `3` | Reviews needed to count as present |
| `many_reviews` | `25` | Reviews needed for `has_many_reviews` |
| `data_quality_min_fields` | `4` | Fields needed for `data_quality_high` |
| `hot_score` | `70` | Score at or above which priority is `hot` |
| `warm_score` | `45` | Score at or above which priority is `warm` |
| `cold_score` | `20` | Score at or above which priority is `cold` |
| `min_confidence_for_priority` | `low` | Minimum confidence required to award `hot` |

### Confidence

```yaml
confidence:
  high_min_signals: 7      # 7 or more signals present -> high
  medium_min_signals: 4    # 4 or more -> medium, otherwise low
```

## Customising business types

`business_types.yaml` maps a term in any language to OpenStreetMap tags:

```yaml
categories:
  restaurants:
    category: food
    aliases: ["restaurants", "restaurant", "مطاعم", "مطعم"]
    tags: ["amenity=restaurant", "amenity=fast_food"]
fallback_tags: ["shop=*"]
```

Add a category, or an Arabic alias your team actually uses, and it becomes
available to `--type` immediately. Unmatched terms fall back to `fallback_tags`.

## Customising website checks

`website_signals.yaml` drives the HTTP checker:

| Key | Purpose |
| --- | --- |
| `http.ok_status` | Statuses treated as "site exists" |
| `http.weak_status` | Statuses treated as "exists but degraded" (401, 403, 5xx) |
| `http.missing_status` | Statuses treated as "page does not exist" (404, 410) |
| `social_domains` | Hosts that count as social, not a website |
| `weak_hosts` | Free website builders that count as a weak presence |
| `shop_markers` | Strings indicating an online store |
| `contact_markers`, `contact_paths` | How a contact page is detected |
| `min_content_length` | Below this, a page counts as thin |
| `placeholder_markers` | Parked-domain and "coming soon" text |
| `outdated_after_years` | Age after which a stale copyright year counts as outdated |

## Deployment notes

- **Never commit `.env`.** It is already in `.gitignore`.
- Set a real contact address in `LEAD_FINDER_USER_AGENT` before any sustained
  use of OpenStreetMap services. It is a use-policy requirement, not decoration.
- Search results are cached in SQLite, so re-running a search is cheap: existing
  rows are refreshed in place rather than inserted again.