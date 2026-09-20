# Dashboard / Control Center

A local web UI over the agents the CLI already uses. Start it with:

```bash
lead-finder dashboard                 # http://127.0.0.1:8765
lead-finder dashboard --port 9000
lead-finder dashboard --open          # open a browser once it is listening
```

The dashboard is a **presentation and control layer**. It does not reimplement
discovery, checking, scoring or analysis. Every action it can take is dispatched
through the existing
[`AgentManager`](../lead_finder_agent/core/manager.py), and every read goes
through the existing repository and agent APIs.

---

## Why it is built this way

Three constraints shaped the implementation, and each is visible in the code.

**The Agent Manager stays the orchestrator.** The dashboard does not create a
second registry. It asks
[`AgentManager.register_default_agents()`](../lead_finder_agent/core/manager.py)
which agents exist and joins that with stored configuration. An agent that
already exists in code therefore appears in the UI even if it has never been
configured, and adding an agent later needs no dashboard change.

**Each run gets its own `AgentContext`.** `AgentContext` owns a
`SQLiteLeadRepository`, which holds a single sqlite connection. The server is
threaded, so sharing one context between concurrent jobs would mean sharing one
connection across threads. A job builds a private context; reads share a separate
context behind a lock. Thread-safety is a property of the dashboard rather than
something sqlite has to tolerate.

**Nothing secret ticks through the presentation layer.** The API returns a masked
preview and a boolean. The frontend never receives a value, and the tests assert
that a configured secret appears in no response, no log record, and no file the
dashboard writes.

---

## Sections

| Section | Source |
| --- | --- |
| Overview | `repository.find()` + `repository.count()` + the job store + `health()` |
| Agent Manager | `AgentManager.agents()` joined with `AgentConfigStore` |
| Credentials & Providers | the credential catalog + `SecretStore.describe()` |
| Leads | `repository.find(LeadFilter(...))` |
| Runs & Jobs | the job store |
| Settings | the runtime settings overlay over `Settings` |

### Overview

Lead totals, average score, score bands, website statuses, priorities, sources,
agent statuses, run counts, system health and recent errors.

Website status is reported as-is, including `website_unknown`. The page states
plainly that *unknown* means the check was inconclusive — it is not a claim that
the business lacks a website. That distinction is the project's central honesty
invariant and the dashboard does not blur it.

### Agent Manager

Lists every agent with its status, and offers enable/disable, configuration, and
run. Disabling records intent; the service refuses to route work to a disabled
agent and returns `409`, rather than pretending the run happened.

### Agent configuration

Per agent: system instructions, agent-specific settings, allowed tools, and
reset-to-defaults. Model selection and runtime settings appear only where the
existing agent actually supports them — the shipped agents are deterministic and
take no model, so the dashboard does not invent a model picker.

A credential-shaped settings key (`api_key`, `secret`, `token`, `password`,
`credential`) is **rejected with `400`**. Credentials live in one central place,
and silently accepting a key here would break that rule quietly.

### Credentials & Providers

One area, organized by channel, on behalf of the Agent Manager:

| Channel | Entries |
| --- | --- |
| Search | `osm`, `sample` (keyless), `google_places` (keyed) |
| LLM | OpenAI, Anthropic, custom/self-hosted |
| Email | SMTP |
| Messaging | WhatsApp Business API |

Search providers are read from the project's own provider registry, so a provider
added to the code appears here without editing the dashboard. The reserved
channels are declared so the architecture exists before an agent needs it; they
are labelled as reserved rather than implied to work.

### Leads

Search by name, and filter by city, country, type, source, priority, website
status and minimum score. Order by score, discovery date, name, city or
priority. Paginated, with a detail view showing contact fields, scoring reasons,
the score breakdown and any stored Website Analyzer findings.

Filter values come from the data (`/api/leads/facets`), so the UI offers real
choices rather than a hard-coded list.

### Runs & Jobs

Recent, running, completed and failed runs with pipeline stage counters
(raw → normalized → duplicates removed → checked → scored → stored), per-provider
results and errors. A run that failed before storing anything is still recorded:
the history is kept separately from the lead database precisely so failures
remain visible.

### Settings

Non-secret runtime configuration as an overlay applied on top of `Settings`:
general defaults, network timeouts, website-checker limits, OpenStreetMap tuning
and the non-secret Google Places knobs. The schema is a whitelist, and **none of
the exposed fields is a credential**, so the overlay has nothing secret to leak.
Clearing a field restores the underlying default.

---

## Credential handling

Precedence:

1. **Environment variables.** If a variable is set it wins, and the UI reports
   the source as `environment`. This keeps the project's env-first design and
   means a deployment can inject credentials the dashboard cannot overwrite.
2. **A local file** under the dashboard data directory, written mode `0600`, for
   values entered through the UI.

### Naming a credential

A write accepts either a provider key (`google_places`) or the environment
variable itself (`GOOGLE_PLACES_API_KEY`). Both resolve to the single canonical
name declared in the catalog, which is the variable the provider reads at run
time. The UI posts the provider key because that identifier is stable in the
interface, so this resolution is what connects a saved key to the component that
uses it.

Only names the catalog declares are accepted. An unknown name, an empty name, or
a provider that needs no key is **rejected with `400`**. Accepting arbitrary
names would let a caller store an unused value and read back a success that
means nothing, and it would widen the set of names the secret-leak tests have to
reason about.

Guarantees, each covered by a test:

- The API returns `{configured, source, preview}`. The preview is eight bullets
  plus, for long values only, the last four characters.
- `SecretStore.get()` is the only method that returns a raw value, and it exists
  solely for a component that genuinely needs the credential.
- Error text is scrubbed through `SecretStore.redact()` before it is returned or
  logged.
- The secrets file is created `0600` and replaced atomically.
- `data/dashboard/` and `secrets.json` are gitignored.
- The frontend uses no `localStorage`, `sessionStorage` or `document.cookie`, and
  calls no `console.log`.
- `.env.example` ships every credential as an empty value.

The broad test iterates every endpoint and every secret name the catalog
declares, so a credential added in future is covered automatically.

---

## API

All endpoints are JSON. Not-found resources are `404`, invalid input `400`,
disabled agents `409`.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api` | Index of endpoints and version |
| GET | `/api/health` | Database, data directory and provider availability |
| GET | `/api/overview` | Landing-page aggregates |
| GET | `/api/agents` | Registered agents with configuration |
| GET | `/api/agents/{name}` | One agent |
| PUT | `/api/agents/{name}` | Update instructions, settings, tools, enablement |
| POST | `/api/agents/{name}/reset` | Reset to defaults |
| POST | `/api/agents/{name}/run` | Dispatch a run for that agent |
| GET | `/api/credentials` | Providers with masked credential state |
| PUT | `/api/credentials/{name}` | Store a credential centrally |
| DELETE | `/api/credentials/{name}` | Remove a locally stored credential |
| GET | `/api/leads` | Filtered, paginated leads |
| GET | `/api/leads/facets` | Distinct filter values |
| GET | `/api/leads/{lead_id}` | Full lead detail |
| GET | `/api/runs` | Run history and counts |
| GET | `/api/runs/{job_id}` | One run |
| POST | `/api/runs/search` | Queue a Lead Finder run |
| POST | `/api/runs/analyze` | Queue a Website Analyzer run |
| GET | `/api/settings` | Effective non-secret settings |
| PUT | `/api/settings` | Update the settings overlay |

Request bodies are capped at 64 KB, query values at 512 characters.

---

## Security

- **Loopback by default.** Binding a non-loopback interface requires
  `--allow-remote`. There is no authentication, so exposing the dashboard on a
  network is an explicit, informed choice.
- **Static files are resolved and then checked for containment**, so `../`
  cannot escape the static directory. A request for a missing page falls back to
  the shell; a missing asset is `404`.
- **Responses are not cached** (`Cache-Control: no-store`), so a revoked
  credential cannot be served from a cache. `X-Content-Type-Options: nosniff` and
  `Referrer-Policy: no-referrer` are set.
- **Everything interpolated into the DOM is escaped**, because business names and
  provider error text originate outside this system.

---

## Local files

Written under `<database directory>/dashboard/`:

| File | Contents | In version control |
| --- | --- | --- |
| `secrets.json` | Credentials entered through the UI, mode `0600` | Never |
| `agent_config.json` | Per-agent instructions, settings, tools, enablement | No |
| `runs.json` | Run history (bounded, 100 records) | No |
| `settings.json` | Non-secret settings overlay | No |

A corrupt file is not fatal: the dashboard logs a warning and starts from
defaults. A run left `running` by a crash is reported as `failed`, because the
alternative is a job that appears to run forever.

---

## Tests

`tests/unit/test_dashboard_security.py`, `tests/unit/test_dashboard_api.py`,
`tests/unit/test_dashboard_cli.py` and
`tests/integration/test_dashboard_server.py`.

The server tests drive a real `ThreadingHTTPServer` on an ephemeral loopback
port, so request parsing, static serving, path-traversal rejection and security
headers are exercised as a client would see them. All tests run offline.