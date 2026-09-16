# Website checker

The checker answers one question per business: **does this business have a
usable website, and what state is it in?**

It is deliberately the only component that makes that call. Search providers
answer "what businesses exist?" and never classify a website; the checker never
searches for businesses. Either side can be replaced without touching the other.

## The rule that shapes everything

**A failure to verify is not proof of absence.**

If the checker cannot reach a website, or no source supplied a URL, the result is
`website_unknown` — not `website_not_found`. `website_not_found` is reserved for
the one case where a server actually tells us the page is gone (an HTTP 404 or
410 for a URL that a source supplied).

This distinction is the entire value of the tool. "We could not confirm a
website" and "this business has no website" lead to different sales actions, and
conflating them produces wrong leads. Every design decision below follows from
it.

| Situation | Status | Why |
| --- | --- | --- |
| Page reached and content read | `website_exists` | Verified |
| URL supplied, request failed (timeout, DNS, TLS, refused) | `website_unreachable` | A URL exists; we could not reach it |
| URL supplied, server returned 404/410 | `website_not_found` | The server confirmed it is gone |
| No URL from any source | `website_unknown` | Absence of data, not absence of a site |
| URL supplied but malformed (`N/A`, `javascript:`, bare text) | `website_unknown` | A bad field is not evidence of no site |
| Domain guessed from the name and it 404s | `website_unknown` | A guess proves nothing either way |
| Social profile in the website field | `website_exists` (`social_only`) | An owned site, not a social page |

## What it checks

For each business, in order:

1. **Validate the URL.** Values are normalized (a bare `example.com` becomes
   `https://example.com`) and unsafe schemes are rejected. `javascript:`,
   `data:`, `file:`, `ftp:`, and `mailto:` URLs are never fetched, and a URL
   carrying embedded credentials (`user:pass@host`) is refused outright.
2. **Social detection.** A website field pointing at Facebook, Instagram, and
   similar is recorded as `social_only` — it is a real presence, but not an
   owned website.
3. **Fetch, with limits.** HTTPS is tried first, then HTTP. Redirects are
   followed up to a cap. The response body is read up to a byte cap.
4. **Grade what came back.** Status codes, page content, and hosting signals
   combine into a quality (`good`, `weak`, `social_only`, `unknown`).

## Safety limits

The checker makes requests to arbitrary third-party sites, so every request is
bounded. This is a security property, not a performance tweak.

| Limit | Setting | Default | Protects against |
| --- | --- | --- | --- |
| Timeout | `WEBSITE_CHECK_TIMEOUT` | `10` s | A site that accepts a connection and never responds |
| Redirect cap | `WEBSITE_MAX_REDIRECTS` | `5` | Redirect loops and unbounded chains |
| Body size cap | `WEBSITE_MAX_RESPONSE_SIZE` | `1000000` bytes | A page streaming unbounded content |
| Per-run cache | `WEBSITE_CACHE_ENABLED` | `true` | Re-requesting the same domain within one run |

A response that hits the size cap is marked `truncated` and still graded from the
bytes that were read; it is never downloaded in full first.

Remote content is never trusted. Titles are stripped of markup and length-capped
before display, and the CLI never prints a raw exception message — only a short,
classified phrase such as "secure connection failed".

## Error classification

Transport libraries report the same problem with different wording, so failures
are mapped onto a stable taxonomy (`website_checker/errors.py`) rather than
pattern-matched at the call site:

`invalid_url`, `timeout`, `dns_failure`, `tls_failure`, `connection_failure`,
`http_error`, `redirect_limit`, `response_too_large`, `malformed_response`,
`unknown`.

Classification is conservative: anything unrecognised becomes `unknown` instead
of being guessed at.

## Identity: how sure are we whose site this is?

Verifying that a URL responds is not the same as verifying it belongs to the
business. Each result carries an `identity` value that states how strong the link
is:

- `provided` — the URL came from the source record. This is the strongest
  evidence available and is never upgraded to a claim of ownership.
- `title_match` — the page title closely matches the business name.
- `uncertain` — the domain was guessed, or nothing corroborates it.
- `not_applicable` — no URL was checked.

A weak hint never promotes a result to `title_match`; the check is conservative
on purpose.

## Result shape

`WebsiteCheckResult` records, in addition to the status and quality:

- `final_url` — where the request actually landed, after redirects
- `http_status`, `response_time_ms` — the response itself
- `error_kind` — the classified failure, when there was one
- `page_title` — the parsed title, if any
- `website_source` — which provider supplied the URL
- `identity`, `truncated`, `from_cache` — provenance and honesty flags

All new fields are additive. A record written before they existed still loads,
with sensible defaults.

## Using it in code

```python
from lead_finder_agent.checker import create_checker, available_checkers

checker = create_checker("http", {"probe_by_name": False})
result = checker.check(lead)
if result.status == WebsiteStatus.UNKNOWN:
    ...  # could not confirm — do NOT treat as "no website"
```

`available_checkers()` lists registered strategies. To add one, implement
`BaseWebsiteChecker.check()` and register it:

```python
from lead_finder_agent.checker import register_checker

register_checker("my-strategy", lambda config: MyChecker(config))
```

The registry refuses to silently overwrite an existing name, so a duplicate
registration fails loudly rather than changing behaviour quietly.

## Running the checks offline

Every test fakes the HTTP transport, so the suite never touches the network and
never depends on a third-party site being up. If you add a checker, follow the
same rule: take an injected client, and let the test supply the responses.

```bash
python -m pytest tests/unit/test_website_checker.py
python -m pytest tests/integration/test_website_checker_integration.py
```
