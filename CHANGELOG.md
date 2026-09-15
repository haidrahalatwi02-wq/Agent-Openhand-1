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

#### Scoring
- Declarative, data-driven rule engine: score, confidence, priority and
  human-readable reasons.
- 18 default rules covering missing and weak websites, business activity,
  contactability and public rating/review signals.
- Configurable thresholds, confidence bands and rule overrides via
  `scoring_rules.yaml` or `LEAD_FINDER_SCORING_RULES`.
- Low-confidence leads can never be presented as hot.

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
- `LeadFinderPipeline`: search → normalize → deduplicate → check → score →
  store → results, with per-stage statistics and failure isolation.
- CLI with `search`, `list`, `show`, `export`, `stats` and `providers`.
- Offline first-run experience via the `sample` provider.

#### Project
- Packaging via `pyproject.toml` with a `lead-finder` console script.
- `Makefile` for install, test, run-example and clean.
- 258 tests (249 offline unit tests plus 9 integration tests), all passing.
- Documentation: README, ARCHITECTURE, CONTRIBUTING, CHANGELOG, and `docs/`
  covering usage, configuration, architecture, data model, providers and
  development.
- MIT license.

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