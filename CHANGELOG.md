# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

#### Search
- Pluggable provider architecture with a registry (`BaseSearchProvider`,
  `register_provider`, `build_providers`).
- `osm` provider: OpenStreetMap discovery through Overpass, geocoded with
  Nominatim. Free and keyless.
- `sample` provider: eight fictional businesses for offline demos and tests.
- `google_places` provider: real business discovery through the official Google
  Places API (New) text search. Keyed, paid, and skipped automatically when the
  key is absent.
- Provider error taxonomy (`ProviderErrorKind`: `invalid_config`, `missing_key`,
  `rate_limited`, `network`, `malformed_response`, `provider_error`) with
  retryability, exposed on each response as `error_kind` and in `--json` output.
- Pagination support in the provider contract: providers report `pages_fetched`,
  follow `nextPageToken` until the requested limit is met, and never return more
  results than `limit`.
- De-duplication of Google Places records on the stable place `id`, with a
  deterministic public-field fallback when an id is absent.
- Google Places configuration: `LEAD_FINDER_GOOGLE_PLACES_KEY_ENV`,
  `LEAD_FINDER_GOOGLE_PLACES_ENDPOINT`,
  `LEAD_FINDER_GOOGLE_PLACES_MAX_PAGES`, `LEAD_FINDER_GOOGLE_PLACES_LANGUAGE`.
  The API key itself is read from the environment inside the provider and is
  never stored on the settings object.
- Multi-provider fan-out with per-provider isolation: one failing source never
  stops a run.
- Business-type resolver with English and Arabic aliases, configurable in
  `business_types.yaml`, with a broad `shop=*` fallback.
- Rate-limit-aware request pacing and a descriptive default User-Agent.

#### Website checking
- `HttpWebsiteChecker` with four honest outcomes: `website_exists`,
  `website_not_found`, `website_unreachable`, `website_unknown`.
- Quality grading: `good`, `weak`, `social_only`, `unknown`.
- Detection of placeholder/parked pages, thin content, stale copyright years and
  free website-builder hosts.
- Social profile detection, so Facebook is never mistaken for a website.
- Contact-page probing at standard paths (opt-in).
- HTTP-only sites are resolved by trying the alternate scheme.
- A guessed domain returning 404 stays `website_unknown` by design; only a URL
  from the source data can yield `website_not_found`.

#### Website checker hardening
- URL validation module (`checker/urls.py`): scheme-less values are completed to
  HTTPS, while `javascript:`, `data:`, `file:`, `ftp:`, `mailto:` and
  credential-bearing URLs are rejected before any request is made.
- Redirect handling: chains are followed to a configurable cap, and a loop or an
  over-long chain is reported as `website_unreachable` with
  `error_kind=redirect_limit` instead of being followed indefinitely.
- Response size cap: a body larger than `WEBSITE_MAX_RESPONSE_SIZE` is truncated
  and flagged `truncated`, never downloaded in full.
- Per-request timing (`response_time_ms`) and the final URL after redirects.
- Structured error taxonomy (`WebsiteErrorKind`) so failures are classified
  rather than pattern-matched at the call site, with short human wording that
  never exposes raw exception text.
- Identity strength (`WebsiteIdentity`: `provided`, `title_match`, `uncertain`,
  `not_applicable`), so a reachable page is never silently claimed as owned.
- Checker registry (`create_checker`, `register_checker`,
  `available_checkers`), so an additional verification strategy can be added
  without changing the pipeline. Duplicate registration fails loudly.
- Per-run URL cache: a repeated domain is requested once per search, and cached
  results are returned as copies so caller edits cannot leak between leads.
- Checker settings: `WEBSITE_CHECK_TIMEOUT`, `WEBSITE_MAX_REDIRECTS`,
  `WEBSITE_MAX_RESPONSE_SIZE`, `WEBSITE_CACHE_ENABLED`.
- The CLI prints a legend under the results table stating that an unconfirmed
  website does not mean the business has no website.

#### Scoring
- Declarative, data-driven rule engine: score, confidence, priority and
  human-readable reasons.
- 22 default rules covering missing and weak websites, business activity,
  contactability and public rating/review signals.
- Configurable thresholds, confidence bands and rule overrides via
  `scoring_rules.yaml` or `LEAD_FINDER_SCORING_RULES`.
- Low-confidence leads can never be presented as hot, and a lead is never `hot`
  unless the data established a website gap: an unverified check is not evidence
  of "no website".
- `hot` requires a confirmed missing, weak or social-only web presence. A
  data-rich record whose website was never verified falls back to `warm`.
- Overriding a rule's points preserves that rule's category and position.

#### Data
- `Lead` model with a stable `dedupe_key`, timezone-aware timestamps and a
  JSON-safe raw payload.
- Normalizer that maps source-specific field aliases, cleans phones, emails and
  URLs, and strips a personal-data denylist before anything is stored.
- De-duplication: exact key matching plus fuzzy name matching within a city and
  conclusive phone matching, merging into the richest available record.

#### Storage
- `BaseLeadRepository` interface and a SQLite backend, so the database can be
  replaced without touching the pipeline.
- Upsert on `dedupe_key` that never blanks out existing data.
- Filtering by city, country, type, source, status, priority and score, with
  allow-listed ordering.
- JSON, CSV and TSV export.

#### Agent and CLI
- Agent Core: `BaseAgent` contract and `AgentContext` dependency bundle, built so
  further agents can be added without a rewrite.
- `AgentManager`: registers agents by name, routes work to them and hands every
  agent one shared `AgentContext`, so agents coordinate through a single
  repository instead of importing each other. A duplicate name is refused unless
  `replace=True`.
- Per-agent failure isolation: `run_isolated()` and `run_all()` report a failing
  agent as an `AgentRunResult` with `ok=False` rather than raising, so one broken
  agent cannot discard the work the others completed.
- `LeadFinderPipeline`: search → normalize → deduplicate → check → score →
  store → results, with per-stage statistics and failure isolation.
- CLI with `search`, `list`, `show`, `export`, `stats`, `providers`, `agents` and
  `analyze`.
- Offline first-run experience via the `sample` provider.
- Location resolver: city and country input is folded onto one canonical
  spelling, so `عدن`, `مدينة عدن` and `Aden` are the same search. The country is
  inferred from the city when omitted. Aliases live in `locations.json`.

#### Website Analyzer agent
- `WebsiteAnalyzerAgent` (`agents/website_analyzer.py`): reads the leads already
  stored and turns each stored website check into findings an outreach message
  can use, instead of a single status.
- Twelve finding kinds — `no_website_confirmed`, `social_only_presence`,
  `site_unreachable`, `no_https`, `weak_quality`, `identity_uncertain`,
  `no_contact_details`, `no_shop`, `slow_response`, `truncated_response`,
  `missing_title`, `check_unavailable` — each carrying a severity and a
  human-readable reason.
- Thresholds and severities are rule data in
  `config/data/website_analysis.{yaml,json}`, editable without a code change and
  overridable per call.
- The agent reports the stored check rather than re-checking the site, so its
  output cannot contradict the lead it describes and it makes no network request
  unless `recheck=True` is passed.
- Absent data is not a negative: a finding about HTTPS, a contact page, a shop,
  response time, truncation or the title is emitted only when the stored payload
  actually recorded that field, so an older or partial record produces no claim
  about details nobody checked.
- Read-only by default. Findings are returned, and written back only under
  `store=True`. They never change a lead's score.
- Registered with `AgentManager` through a lazy import in
  `register_default_agents()`, so the manager stays decoupled and no import cycle
  forms.
- `lead-finder analyze` exposes it from the CLI, with `--limit`, `--lead-id`,
  `--status`, `--min-severity`, `--recheck`, `--store` and `--json`.

#### Project
- Packaging via `pyproject.toml` with a `lead-finder` console script.
- `Makefile` for install, test, run-example and clean.
- 913 tests (879 offline unit tests plus 34 integration tests), all passing.
- Documentation: README, ARCHITECTURE, CONTRIBUTING, CHANGELOG, and `docs/`
  covering usage, configuration, architecture, data model, providers, website
  checker, scoring, website analyzer, dashboard and development.
- MIT license.

#### Dashboard / Control Center
- `lead-finder dashboard` serves a local web UI over the existing agents, on
  `127.0.0.1:8765` by default. A non-loopback bind requires `--allow-remote`.
- Seven sections: Overview, Agent Manager, Agent configuration, Credentials &
  Providers, Leads, Runs & Jobs, and Settings.
- Built on the standard library only (`http.server`, `json`, `urllib`), keeping
  the project's single runtime dependency. No web framework was added.
- The dashboard dispatches every action through the existing `AgentManager`; it
  does not register a second agent system. Disabling an agent is enforced by the
  service and returns `409` rather than silently skipping the run.
- `dashboard/credentials.py` derives search providers from the project's own
  provider registry, so a provider added later appears without a dashboard
  change, and declares the reserved LLM, email and WhatsApp channels.
- Central credential handling: `SecretStore` resolves from the environment first
  and then from a local file written mode `0600`. The API returns only a masked
  preview and a boolean, error text is scrubbed through `redact()`, and the
  frontend receives no value. Credential-shaped keys are rejected on the
  per-agent settings endpoint with `400`.
- Per-agent configuration for instructions, settings, allowed tools and
  enablement, with reset-to-defaults.
- Bounded run history (`runs.json`, 100 records) recording status, pipeline
  stage counters, per-provider results and errors. A run interrupted by a
  restart is reported as `failed` rather than left `running` forever.
- Editable non-secret settings overlay over `Settings`, with a whitelist schema
  containing no credential field.
- Responsive UI with loading, empty and error states throughout; the frontend
  uses no browser storage and escapes everything interpolated into the DOM.
- 165 new tests: the API, agents, leads, credentials, CLI gate, a real
  `ThreadingHTTPServer` on an ephemeral loopback port, and secret-leak checks
  that iterate every endpoint against every declared secret name.
- `.gitignore` excludes `data/dashboard/` and `secrets.json`; the dashboard's
  static assets ship in the wheel via `package-data`.

### Fixed

- Saving a credential in the dashboard reported success but stored it under the
  provider key (`google_places`) instead of the variable the provider reads
  (`GOOGLE_PLACES_API_KEY`). The key never reached the provider, and the UI then
  displayed that provider as unconfigured. A write now resolves either form to
  the one canonical name, and rejects names the catalog does not declare.
- A keyless provider was reported as having a stored credential, so the UI
  rendered a masked value that did not exist. Keyless providers now report no
  credential state.
- A run started while a read was in flight on a fresh database could fail with
  `database is locked`, because both raced to write the schema. Repository
  creation now happens under the read lock, which leaves the run itself
  concurrent with reads.
- Arabic city names returned no results. `--city عدن` searched for the literal
  string while records are stored as `Aden`, so a documented input silently
  found nothing. City and country input is now resolved before searching, and
  `list` and `export` resolve their location filters the same way so a localized
  filter no longer disagrees with the search that produced the data.
- The `sample` provider ignored `--country`, returning records for the wrong
  country. It now excludes records whose country disagrees with an explicit
  `--country`.
- A canonical category (`restaurants`) never matched a provider record using the
  singular form (`restaurant`). Singular and plural are now the same vertical.
- `--limit 0` silently became 50 because `0` was replaced by the default via
  `limit or 50`. A non-positive limit now raises instead of misreporting how
  many results were requested.

### Notes
- `website_unknown` is an expected outcome, not an error. It means the check was
  inconclusive.
- Only public business data is collected. See
  [docs/data-model.md](docs/data-model.md).

## [0.1.0] - 2026-09-14

Initial MVP release: the first working Lead Finder Agent, from search through to
exported, scored leads.

[Unreleased]: https://github.com/haidrahalatwi02-wq/Agent-Openhand-1/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/haidrahalatwi02-wq/Agent-Openhand-1/releases/tag/v0.1.0