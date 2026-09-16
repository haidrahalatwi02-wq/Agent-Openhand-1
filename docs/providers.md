# Search providers

Providers turn a `SearchQuery` into raw business records. They are the only part
of the system that talks to the outside world during discovery, and the only part
you replace when you need new data sources.

## Built-in providers

| Name | Key needed | Network | Notes |
| --- | --- | --- | --- |
| `google_places` | `GOOGLE_PLACES_API_KEY` | Yes | Real businesses via the official Google Places API (New). Paid |
| `osm` | No | Yes | OpenStreetMap via Overpass, geocoded with Nominatim |
| `sample` | No | No | Fictional bundled data for demos and tests |

```bash
lead-finder providers          # list them, with availability
lead-finder search --country Yemen --city Aden --type restaurants --providers google_places
lead-finder search --city Aden --type restaurants --providers osm
lead-finder search --city Aden --type restaurants --providers sample
lead-finder search --city Aden --type restaurants --providers google_places,osm,sample
```

A provider whose key is missing is not an error. It is skipped with a clear
reason, and every other provider still runs:

```
note: provider google_places skipped: google_places requires an API key:
set GOOGLE_PLACES_API_KEY or pass 'api_key' in the provider config
```

### `google_places` — Google Places API (New)

The first provider that returns real business data. It calls Google's official
`places:searchText` endpoint, which is documented, supported and used under
Google's terms of service.

It searches on exactly what the caller supplies — country, city/region,
business type/category, keywords and a result limit — and hard-codes no country
or city:

```bash
lead-finder search --country Yemen --city Aden --type restaurants --limit 20
lead-finder search --country Portugal --city Lisbon --type bakeries --limit 20
```

Setup:

```bash
export GOOGLE_PLACES_API_KEY="your-key-here"
export LEAD_FINDER_PROVIDERS=google_places,osm,sample
lead-finder providers
```

Fields captured (only what Google returns, and only public business data):
name, category/type, address, city, country, public phone, website, coordinates,
rating and review count, business status, and a Google Maps source URL. A field
Google does not return is left absent rather than invented.

**Pagination.** Google caps `pageSize` at 20 and returns a `nextPageToken`. The
provider follows that token until the caller's `--limit` is satisfied, then
stops. It never returns more results than requested, even if the API sends more.
`LEAD_FINDER_GOOGLE_PLACES_MAX_PAGES` (default 3) caps total requests.

**Deduplication.** Records are de-duplicated on Google's stable place `id`
within and across pages. If an id is somehow absent, a deterministic key is
derived from public fields (name, address, city, country) so a repeated record
never becomes a repeated lead.

#### Limitations of this data source

Be aware of these before relying on it:

- **It costs money.** Places API (New) is billed per request and needs a Google
  Cloud project with billing enabled. There is no free tier to fall back on.
- **It requires a key, and keys are per-project.** A key restricted to the wrong
  API or missing the Places API (New) enabled returns HTTP 400/403.
- **Google's terms apply.** Results are subject to Google Maps Platform terms,
  including caching and attribution rules that are stricter than raw HTTP. Read
  them before storing or redistributing results.
- **Coverage is uneven.** Google is strong in dense urban areas and weaker in
  rural or newly mapped places. It is not a complete business registry.
- **Text search is fuzzy.** `textQuery` matches on text, so it can return a
  nearby or loosely related business. It is not a strict category filter.
- **Roughly 60 results per query.** Text search does not page indefinitely;
  `max_pages` defaults low because of that.
- **It is not a website-quality oracle.** `rating` and `review_count` come from
  Google; the website check is a separate, later pipeline stage.
- **Phone numbers are business numbers as published by Google.** The provider
  never contacts a business; there is no automated outreach anywhere in this
  project.

### `osm` — OpenStreetMap

Uses two free public services:

- **Nominatim** resolves `city` + `country` to a bounding box.
- **Overpass** returns nodes and ways inside that box matching the tags for the
  requested business type.

The provider:

- Maps a business type or Arabic alias to Overpass tags using
  `config/data/business_types.yaml`.
- Drops records with no name, since an unnamed business is not actionable.
- Moves social URLs out of the `website` tag into `social_links`, so Facebook is
  never mistaken for a website.
- Records `source_url` as a link back to the OpenStreetMap element.

Failures are reported, never raised. The provider returns a `skipped_reason`
with a clear cause when:

- the city cannot be geocoded,
- Overpass returns an error, times out, or returns unparseable JSON,
- no location was supplied at all.

**Be a good citizen.** Overpass and Nominatim are community services with usage
policies. Set a descriptive `LEAD_FINDER_USER_AGENT` with a real contact, keep
`--limit` reasonable, and do not run tight loops against them.

### `sample` — offline data

Eight fictional businesses across several categories in one city. It exists so
that demos, `make run-example` and the test suite never touch the network. It has
no real domains, so with the website check enabled its leads are correctly
reported as `website_unknown`.

## Adding a provider

Subclass `BaseSearchProvider`, implement `search_raw`, and register it.

```python
# lead_finder_agent/search/providers/my_source.py
from lead_finder_agent.search.base import (
    BaseSearchProvider,
    ProviderError,
    ProviderErrorKind,
)
from lead_finder_agent.search.registry import register_provider


@register_provider(name="my-source")
class MySourceProvider(BaseSearchProvider):
    name = "my-source"
    description = "My data source"
    requires_key_env = "MY_SOURCE_API_KEY"   # optional

    def search_raw(self, query):
        # Return raw dicts. Field names do not need to match the Lead model;
        # the normalizer maps common aliases for you.
        response = self.client.get(
            "https://api.example.com/search",
            params={"q": query.business_type, "city": query.city},
        )
        if response.status_code == 429:
            raise ProviderError("rate limited", kind=ProviderErrorKind.RATE_LIMITED)
        if not response.ok:
            raise ProviderError(
                f"API error: {response.error or response.status_code}",
                kind=ProviderErrorKind.PROVIDER_ERROR,
            )

        return [
            {
                "source_id": item["id"],
                "name": item["business_name"],
                "category": item["type"],
                "phone": item.get("phone"),
                "website": item.get("url"),
                "city": query.city,
                "country": query.country,
            }
            for item in response.json().get("results", [])
        ]
```

Then enable it:

```bash
export LEAD_FINDER_PROVIDERS=my-source,osm,sample
lead-finder providers
```

If your provider pages through an API, call `self._note_page()` for each request
so the run can report how many calls it cost, and stop as soon as you have
`query.limit` results. See
`lead_finder_agent/search/providers/google_places.py` for a worked example.

### What a provider must supply for merging

The merge layer is provider-agnostic: a new source needs no changes to
`search/multi.py`, `extraction/deduplicator.py` or `models/lead.py`. Two things
make merging work well, though:

- **`source_id`** - the provider's own stable id for the record. It becomes the
  `dedupe_key` within that provider and is stored in `provider_ids[<provider>]`.
  Ids are namespaced per provider and never compared across providers, so the
  value only has to be unique inside your source.
- **Public contact and location fields** - `phone`, `website_url`, `address`,
  `latitude`/`longitude`, `city`. These are the independent signals used to
  recognise the same business from two providers; the more of them you return,
  the better the cross-provider match, and the lower the chance of a
  false-positive merge.

Two providers describing one business are merged into a single lead that keeps
the useful data from both and lists every provider in `sources`. Your provider's
values are never overwritten by an empty one from another source.

If your provider has no stable id, the fallback key is
`name + city + phone`, so supply as many of those as you have.

### The provider contract

| Member | Purpose |
| --- | --- |
| `name` | Registry name; also recorded as `Lead.source` |
| `description` | Shown by `lead-finder providers` |
| `kind` | `api`, `sample` or `custom` |
| `requires_key_env` | Environment variable that must be set for the provider to run |
| `search_raw(query)` | Return raw records, or raise |
| `search(query)` | Wraps `search_raw` with timing and error capture — rarely overridden |
| `is_available()` | False when a required key is missing |
| `unavailable_reason()` | Human-readable reason for `providers` output |

### Raising vs returning

| Action | Result |
| --- | --- |
| Return a list | Normal path; the pipeline normalizes it |
| `raise ProviderSkip("reason")` | The provider is skipped; the reason is shown, other providers continue |
| `raise ProviderError(msg, kind=...)` | A classified failure; reported with its kind, other providers continue |
| Any other exception | Captured as a generic provider error, other providers continue |

`ProviderSkip` is for expected, explainable conditions — a missing key, a service
outage, an ungeocodable city. Anything else is a bug worth seeing in the logs.

### Error taxonomy

Every failure carries a machine-readable `kind` so a caller can react to the
*type* of problem instead of string-matching a message. The kind is exposed on
each `ProviderResponse` as `error_kind`, appears in `--json` output, and never
contains a credential.

| `ProviderErrorKind` | Meaning | Retryable | Typical cause |
| --- | --- | --- | --- |
| `invalid_config` | Misconfigured or rejected call | No | HTTP 400/401/403, wrong or restricted key, API not enabled |
| `missing_key` | Required credential absent | No | `GOOGLE_PLACES_API_KEY` unset |
| `rate_limited` | Quota or rate limit hit | Yes | HTTP 429, billing quota exhausted |
| `network` | Transport failure | Yes | DNS, TLS, timeout, connection reset |
| `malformed_response` | Source answered unusably | No | Invalid JSON, `places` not a list |
| `provider_error` | Anything else | No | Unexpected HTTP status |

The kinds are kept apart on purpose, because they call for different actions: a
missing key is the operator's to fix, a rate limit is worth retrying later, a
malformed response means the source changed, and "no results" is a perfectly
successful search — represented by `ProviderResponse.empty`, not by an error.

A `rate_limited` failure is the only one where retrying makes sense. The provider
does **not** retry in-process, because doing so against a quota would deepen the
problem; use `LEAD_FINDER_MAX_RETRIES` deliberately if you want that.

### Response format

Return a list of dictionaries. Recognised aliases include:

| Target field | Accepted keys |
| --- | --- |
| `business_name` | `business_name`, `name`, `title`, `display_name` |
| `business_type` | `business_type`, `type`, `category`, `amenity`, `shop` |
| `phone` | `phone`, `telephone`, `phone_number` |
| `email` | `email`, `contact_email` |
| `website_url` | `website_url`, `website`, `url`, `web` |
| `address` | `address`, `addr`, `street`, `full_address` |
| `description` | `description`, `about`, `summary` |
| `latitude` / `longitude` | `latitude`, `lat` / `longitude`, `lon`, `lng` |
| `rating` / `review_count` | `rating` / `review_count`, `reviews` |
| `source_id` | `source_id`, `id`, `osm_id` |

Unknown keys are preserved in `raw`, so no source data is lost. Keys on the
personal-data denylist are dropped before storage.

### Testing a provider

Inject a fake HTTP transport, as the existing tests do. Never rely on the network
in the default test run.

```python
from lead_finder_agent.utils.http import HttpClient
from tests.conftest import FakeTransport

transport = FakeTransport().add("api.example.com", body='{"results": []}')
provider = MySourceProvider({"client": HttpClient(transport=transport)})
assert provider.search(SearchQuery(city="Aden", limit=5)).ok
```

## Choosing providers

| Situation | Suggestion |
| --- | --- |
| First run, no network | `--providers sample` |
| Real business data, key available | `--providers google_places` |
| Free, keyless discovery | `--providers osm` |
| Demos and tests | `--providers sample` |
| Broader coverage | `--providers google_places,osm,sample`, then add your own source |

The multi-provider engine deliberately over-fetches and then de-duplicates, so
records found in several sources merge into one enriched lead rather than
producing repeats. Leads are keyed on `source:source_id` when a provider supplies
one, and on normalized name + city + phone otherwise.