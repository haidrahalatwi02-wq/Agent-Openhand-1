# Lead scoring

Lead scoring turns a normalized business and its verified website check into a
single number from 0 to 100, plus the reasons behind it. It is the last stage of
the pipeline:

```
Search -> Merge & Deduplicate -> Website Checker -> Lead Scoring -> Lead Results
```

The goal is not to guess. A score describes *how good a prospect this business is
for a new website*, using only what was actually collected and verified. Every
number is reproducible: the same lead and the same check always produce the same
score, reasons and breakdown.

## The honesty invariant

The single most important rule in this layer:

> **A failed or skipped website check is not evidence that a business has no
> website.**

`NOT_FOUND` means a completed check established that no site exists. `UNKNOWN`,
`UNREACHABLE` and `NOT_CHECKED` mean *we could not tell*. They are kept strictly
distinct, and they score differently:

- Only `NOT_FOUND` fires the `no_website` rule (+35).
- An unverified record scores `no_website_information` (+5) or
  `website_unknown_conservative` (+2) instead.

A related consequence is that a lead is never labelled `hot` unless its data
actually established a website gap. See [Priority](#priority).

## Signals

The scorer does not read the raw lead directly. `build_signals()` reduces the
lead and the check result to a flat dictionary of booleans and numbers, and rules
match against that. This keeps the rules declarative and testable.

Signals fall into four groups:

| Group | Signals |
| --- | --- |
| Website opportunity | `website_missing`, `website_unknown`, `website_unreachable`, `website_null`, `website_status_known` |
| Website condition | `has_website`, `website_reachable`, `website_is_good`, `website_is_weak`, `website_social_only` |
| Business relevance | `has_business_name`, `has_business_type`, `business_active`, `business_closed`, `business_active_or_closed` |
| Data completeness | `has_phone`, `has_email`, `has_address`, `has_location`, `has_social`, `has_description`, `has_categories`, `has_source` |
| Reputation | `has_reviews`, `has_good_rating`, `has_many_reviews` |
| Data quality | `data_quality_high`, `data_quality_low`, `filled_field_count` |

`website_status_known` is true only for a *conclusive* check (`EXISTS` or
`NOT_FOUND`). An inconclusive check contributes no confidence: "we did not check"
is not knowledge.

The full signal reference lives in
[docs/configuration.md](configuration.md#available-signals); the signal list is
generated from the same code, so the two stay aligned.

## Rule format

Rules are data, not code, and live in
`lead_finder_agent/config/data/scoring_rules.yaml` (with a `.json` mirror that
must stay in sync). Each rule has an `id`, `points`, an optional `description`
and a `when` mapping:

```yaml
- id: no_website
  points: 35
  description: "No website found - prime candidate for a new website"
  when:
    website_missing: true
```

A rule fires only when **every** condition in `when` holds. Condition semantics:

- **Booleans match exactly.** `{"website_missing": true}` requires the signal to
  be `True`; a missing signal does not satisfy it.
- **Numbers are thresholds meaning "at least".** `{"rating": 4.0}` fires at 4.0
  or above and never fires when the value is missing.
- **Anything else must be equal.**
- **Duplicate rule ids are rejected** when the rule set is loaded.

The points of every rule that fired are summed. Each fired rule with a
description also contributes a human-readable reason.

## Default rules

The packaged set is version **2** and contains 22 rules.

| Rule | Points | Fires when |
| --- | ---: | --- |
| `no_website` | +35 | `website_missing` |
| `weak_website` | +22 | `website_is_weak` |
| `social_only_presence` | +18 | `website_social_only` |
| `website_unreachable_conservative` | +8 | `website_unreachable` |
| `no_website_information` | +5 | `website_null` |
| `website_unknown_conservative` | +2 | `website_unknown` |
| `business_active` | +15 | `business_active` |
| `has_business_type` | +4 | `has_business_type` |
| `has_phone` | +12 | `has_phone` |
| `has_social` | +8 | `has_social` |
| `has_email` | +6 | `has_email` |
| `has_address` | +6 | `has_address` |
| `has_description` | +5 | `has_description` |
| `has_location` | +4 | `has_location` |
| `has_categories` | +4 | `has_categories` |
| `has_source` | +3 | `has_source` |
| `has_good_rating` | +8 | `has_good_rating` |
| `has_many_reviews` | +7 | `has_many_reviews` |
| `data_quality_high` | +5 | `data_quality_high` |
| `good_website` | -45 | `website_is_good` |
| `business_closed` | -50 | `business_closed` |
| `data_quality_low` | -10 | `data_quality_low` |

The asymmetry is deliberate. A confirmed missing website is the strongest
positive signal; a good working website is a strong negative one. Note that
`no_website` (+35) and `no_website_information` (+5) are far apart precisely
because one is evidence and the other is the absence of it.

## Thresholds

Thresholds live alongside the rules and tune both the signals and the priority
buckets:

| Threshold | Default | Meaning |
| --- | ---: | --- |
| `good_rating` | 4.0 | Rating that counts as good |
| `reviews_present` | 3 | Reviews above which `has_reviews` is true |
| `many_reviews` | 25 | Reviews at which `has_many_reviews` is true |
| `data_quality_min_fields` | 4 | Filled fields for high data quality |
| `hot_score` | 70 | Score at or above which a lead is `hot` |
| `warm_score` | 45 | Score at or above which a lead is `warm` |
| `cold_score` | 20 | Score below which a lead is `disqualified` |
| `min_confidence_for_priority` | `low` | Minimum confidence for `hot`/`warm` |

## Score

Points are summed and then clamped to `score_min`..`score_max` (0..100 by
default). A record that can earn more than 100 is clamped, so
`sum(breakdown) >= score` whenever clamping applied. `score_breakdown` lists
exactly the rules that fired and their points, which makes any score auditable
after the fact.

## Confidence

Confidence answers "how much do we actually know about this lead?", never
"how sure are we that it has no website?". It is derived from how many knowledge
signals are present:

| Band | Condition |
| --- | --- |
| `high` | at least `high_min_signals` (7) known signals |
| `medium` | at least `medium_min_signals` (4) known signals |
| `low` | otherwise |

A phone number counts twice, because a record with a phone is meaningfully
actionable even when thin.

## Priority

Priority is the bucket a lead is filed under, derived from the score, the
confidence and the signals:

| Priority | Condition |
| --- | --- |
| `disqualified` | Business is closed, or the score is below `cold_score` |
| `hot` | Score at or above `hot_score`, confidence is sufficient, **and** the data established a website gap |
| `warm` | Score at or above `warm_score` |
| `cold` | Everything else |

Two guard rails matter more than the numbers:

1. **A closed business is never viable.** It is decided as an explicit rule
   rather than relying on the -50 penalty, so a rich record can never lift a dead
   business into `warm`.
2. **A lead is never `hot` without an established website gap.** `hot` means
   prime prospect for a *new* website, so it requires evidence: a server-confirmed
   missing site (`NOT_FOUND`), a weak or parked site, or a social-only presence.
   An unverified check (`UNKNOWN`, `UNREACHABLE`, `NOT_CHECKED`) and a good
   working website are both excluded — such a lead is filed as `warm` at most.

That second rule is why a data-rich business whose website was never verified
does not become `hot` merely by scoring high on completeness.

Low confidence also suppresses `hot` and `warm`: when confidence is below
`min_confidence_for_priority`, those candidates fall back to `cold`. Flimsy data
never gets promoted.

## Determinism

The scorer must be reproducible. It does not call an LLM, use randomness, or read
the wall clock. Given the same lead and the same check, the score, reasons and
breakdown are always identical. `LeadScorer` implements the `BaseLeadScorer`
contract in `lead_finder_agent/scoring/base.py`, so a different strategy can be
substituted without touching the pipeline.

Leads are ranked with `Lead.sort_key()` — score first, then confidence, then
name and finally the dedupe key. The trailing fields keep ordering stable when
two leads share a score, so the same data never ranks differently between runs.

## Re-scoring a stored lead

Scoring can run against a stored lead without re-running the checker.
`hydrate_website_check()` recovers the check result from `lead.raw["website_check"]`
and returns `None` for anything missing, empty or corrupt. A payload that cannot
be parsed is discarded rather than guessed at, so corrupt data can never be
mistaken for a conclusive "this business has no website".

## Configuration

Point `LEAD_FINDER_SCORING_RULES` at your own file to override the packaged rules:

```bash
export LEAD_FINDER_SCORING_RULES=/path/to/my-rules.yaml
```

Your file is *merged* onto the defaults, so you only specify what changes. Set
`replace: true` at the top level to discard the defaults entirely. An override
that changes only a rule's points keeps that rule's original position and
category; adding a rule with a new id appends it. See
[docs/configuration.md](configuration.md#customising-scoring) for worked examples.

## Storage and export

`scoring_version` records which rule set produced a score, so an old score stays
interpretable after the rules change. It is stored on the lead and exported with
it, alongside `lead_score`, `score_confidence`, `score_reason`,
`score_breakdown` and `priority`.

## Tests

Scoring behaviour is covered in `tests/unit/test_scoring.py`, organised around
the invariants rather than the implementation:

- `TestUnverifiedIsNotMissing` — `NOT_FOUND` is never confused with `UNKNOWN`
- `TestPrimeProspectPriority` — `hot` requires an established website gap
- `TestPriorityBuckets` / `TestConfidenceBands` — bucket boundaries
- `TestReasonsAndBreakdown` — every fired rule is accounted for
- `TestStoredCheckHydration` — corrupt stored checks are discarded safely
- `TestScoringPersistence` — score and version survive a round-trip

`tests/integration/test_end_to_end.py::TestScoringIntegration` proves scoring
through the real pipeline and storage, with only the HTTP transport stubbed.

## Related documentation

- [docs/website-checker.md](website-checker.md) — how a status is decided, and
  why a failed check is not "no website"
- [docs/configuration.md](configuration.md) — signals, thresholds, rule overrides
- [docs/architecture.md](architecture.md) — where scoring sits in the pipeline
- [docs/data-model.md](data-model.md) — the stored scoring fields
