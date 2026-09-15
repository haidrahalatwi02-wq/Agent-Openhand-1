# Search providers

Providers turn a `SearchQuery` into raw business records. They are the only part
of the system that talks to the outside world during discovery, and the only part
you replace when you need new data sources.

## Built-in providers

| Name | Key needed | Network | Notes |
| --- | --- | --- | --- |
| `osm` | No | Yes | OpenStreetMap via Overpass, geocoded with Nominatim |
| `sample` | No | No | Fictional bundled data for demos and tests |

```bash
lead-finder providers          # list them, with availability
lead-finder search --city Aden --type restaurants --providers osm
lead-finder search --city Aden --type restaurants --providers sample
lead-finder search --city Aden --type restaurants --providers osm,sample
```

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
from lead_finder_agent.search.base import BaseSearchProvider
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
        if not response.ok:
            raise ProviderSkip(f"API error: {response.error or response.status_code}")

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
| `raise ProviderSkip("reason")` | The provider is skipped, the reason is shown, other providers continue |
| Any other exception | Captured as a provider error, other providers continue |

`ProviderSkip` is for expected, explainable conditions — a missing key, a service
outage, an ungeocodable city. Anything else is a bug worth seeing in the logs.

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
| General commercial discovery | `--providers osm` |
| Demos and tests | `--providers sample` |
| Broader coverage | `--providers osm,sample`, then add your own source |

The multi-provider engine deliberately over-fetches and then de-duplicates, so
records found in several sources merge into one enriched lead rather than
producing repeats.