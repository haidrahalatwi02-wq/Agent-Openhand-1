/* Lead Finder Control Center - frontend.
 *
 * Presentation only. Every value comes from /api and every action posts to
 * /api; no business logic lives here.
 *
 * Security note: the server never sends a secret value to this file. Credential
 * responses contain only {configured, source, preview}. This script therefore
 * has nothing secret to hold, and it never writes a credential into the DOM,
 * localStorage, sessionStorage, or a console call.
 */
"use strict";

/* ---------------- tiny helpers ---------------- */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

/* Escape everything interpolated into innerHTML. The API returns business
 * names and provider error text that originate outside this system. */
function esc(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function fmtDate(value) {
  if (!value) return "—";
  const d = new Date(value);
  if (isNaN(d.getTime())) return esc(value);
  return d.toLocaleString(undefined, {
    year: "numeric", month: "short", day: "2-digit",
    hour: "2-digit", minute: "2-digit",
  });
}

function humanise(key) {
  return String(key || "")
    .replace(/^website_/, "")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function toast(message, isError) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.toggle("error", !!isError);
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, isError ? 6500 : 3200);
}

/* ---------------- api client ---------------- */

const api = {
  async request(method, path, body) {
    const options = { method, headers: { Accept: "application/json" } };
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    let response;
    try {
      response = await fetch(path, options);
    } catch (err) {
      throw new Error("Cannot reach the dashboard server. Is it still running?");
    }
    let payload = null;
    const text = await response.text();
    if (text) {
      try { payload = JSON.parse(text); } catch (err) { payload = null; }
    }
    if (!response.ok) {
      const message = (payload && payload.error) || `Request failed (${response.status})`;
      throw new Error(message);
    }
    return payload;
  },
  get(path) { return this.request("GET", path); },
  put(path, body) { return this.request("PUT", path, body); },
  post(path, body) { return this.request("POST", path, body || {}); },
  del(path) { return this.request("DELETE", path); },
};

/* ---------------- view states ---------------- */

function skeleton(rows = 4) {
  let out = '<div class="skeleton" aria-hidden="true">';
  for (let i = 0; i < rows; i++) out += '<div class="bar"></div>';
  out += "</div>";
  return out;
}

function emptyState(title, hint) {
  return `<div class="state"><h3>${esc(title)}</h3><p class="muted">${esc(hint || "")}</p></div>`;
}

function errorState(message) {
  return `<div class="state error"><h3>Something went wrong</h3><p>${esc(message)}</p></div>`;
}

function setView(html) { return html; }

/* ---------------- shared widgets ---------------- */

function badge(text, kind) {
  return `<span class="badge ${esc(kind || "")}">${esc(text)}</span>`;
}

function priorityBadge(priority) {
  const p = String(priority || "cold");
  return badge(p, p);
}

function statusBadge(configured, enabledLabel) {
  if (configured) return badge(enabledLabel || "configured", "enabled");
  return badge("not configured", "disabled");
}

function websiteStatusBadge(status) {
  const s = String(status || "website_unknown").replace("website_", "");
  const kind = s === "exists" ? "good" : s === "unreachable" ? "bad" : s === "not_found" ? "warm" : "unknown";
  return badge(s.replace(/_/g, " "), kind);
}

function statCard(label, value, sub) {
  return `<div class="stat"><div class="label">${esc(label)}</div>
    <div class="value">${esc(value)}</div>
    ${sub ? `<div class="sub">${esc(sub)}</div>` : ""}</div>`;
}

function kv(pairs) {
  const rows = pairs
    .filter(([, v]) => v !== null && v !== undefined && v !== "")
    .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`)
    .join("");
  return `<dl class="kv">${rows}</dl>`;
}

/* ---------------- modal ---------------- */

function openModal(title, bodyHtml) {
  $("#modalTitle").textContent = title;
  $("#modalBody").innerHTML = bodyHtml;
  $("#modalBackdrop").hidden = false;
  $("#modalClose").focus();
}
function closeModal() { $("#modalBackdrop").hidden = true; }

/* ================= OVERVIEW ================= */

async function renderOverview() {
  const body = $("#overviewBody");
  body.innerHTML = skeleton(5);
  let data;
  try {
    data = await api.get("/api/overview");
  } catch (err) {
    body.innerHTML = errorState(err.message);
    return;
  }

  const leads = data.leads || {};
  const bands = leads.score_bands || {};
  const runs = data.runs || {};
  const health = data.health || {};

  const scoreChart = Object.keys(bands)
    .map((k) => `<div class="stat"><div class="label">${esc(k)}</div>
        <div class="value">${esc(bands[k])}</div></div>`)
    .join("");

  const statusRows = Object.entries(leads.by_website_status || {})
    .sort((a, b) => b[1] - a[1])
    .map(([k, v]) => `<tr><td>${esc(humanise(k))}</td><td class="num">${esc(v)}</td></tr>`)
    .join("") || `<tr><td colspan="2" class="muted">No leads stored yet.</td></tr>`;

  const priorityRows = Object.entries(leads.by_priority || {})
    .map(([k, v]) => `<tr><td>${priorityBadge(k)}</td><td class="num">${esc(v)}</td></tr>`)
    .join("") || `<tr><td colspan="2" class="muted">Nothing scored yet.</td></tr>`;

  const sourceRows = Object.entries(leads.by_source || {})
    .map(([k, v]) => `<tr><td>${esc(k)}</td><td class="num">${esc(v)}</td></tr>`)
    .join("") || `<tr><td colspan="2" class="muted">No sources recorded.</td></tr>`;

  const agentRows = (data.agents || []).map((a) => `<tr>
      <td>${esc(a.name)}</td>
      <td>${badge(a.enabled ? "enabled" : "disabled", a.enabled ? "enabled" : "disabled")}</td>
    </tr>`).join("");

  const healthRows = (health.checks || []).map((c) => `<tr>
      <td>${esc(c.name)}</td>
      <td>${badge(c.ok ? "ok" : "problem", c.ok ? "good" : "bad")}</td>
      <td class="mono wrap-anywhere">${esc(c.detail)}</td>
    </tr>`).join("");

  const recentRuns = (data.recent_runs || []).map((r) => `<tr>
      <td class="mono">${esc(r.id)}</td>
      <td>${esc(r.agent)}</td>
      <td>${badge(r.status, r.status === "completed" ? "good" : r.status === "failed" ? "bad" : "unknown")}</td>
      <td>${esc(r.summary || "—")}</td>
      <td>${esc(r.duration_seconds !== null && r.duration_seconds !== undefined ? r.duration_seconds + "s" : "—")}</td>
    </tr>`).join("") || `<tr><td colspan="5" class="muted">No runs recorded yet.</td></tr>`;

  const errors = (data.recent_errors || []).map((e) => `<tr>
      <td>${esc(fmtDate(e.at))}</td>
      <td>${esc(e.agent)}</td>
      <td class="wrap-anywhere">${esc(e.error)}</td>
    </tr>`).join("") || `<tr><td colspan="3" class="muted">No errors recorded.</td></tr>`;

  body.innerHTML = setView(`
    <div class="card">
      <div class="card-head"><h2>Leads</h2><span class="muted small">${esc(leads.scanned || 0)} scanned</span></div>
      <div class="grid">
        ${statCard("Total leads", leads.total || 0)}
        ${statCard("Average score", leads.average_score || 0)}
        ${statCard("With website", (leads.by_website_status || {}).website_exists || 0)}
        ${statCard("No website confirmed", (leads.by_website_status || {}).website_not_found || 0)}
      </div>
      <div class="grid" style="margin-top:12px">${scoreChart}</div>
    </div>

    <div class="grid two">
      <div class="card">
        <div class="card-head"><h2>Website status</h2></div>
        <div class="table-wrap"><table><thead><tr><th>Status</th><th class="num">Leads</th></tr></thead>
          <tbody>${statusRows}</tbody></table></div>
        <p class="muted small" style="margin-top:10px">
          <em>Unknown</em> means the check was inconclusive — it does not mean the business has no website.
        </p>
      </div>
      <div class="card">
        <div class="card-head"><h2>Priority</h2></div>
        <div class="table-wrap"><table><thead><tr><th>Priority</th><th class="num">Leads</th></tr></thead>
          <tbody>${priorityRows}</tbody></table></div>
      </div>
      <div class="card">
        <div class="card-head"><h2>Sources</h2></div>
        <div class="table-wrap"><table><thead><tr><th>Source</th><th class="num">Leads</th></tr></thead>
          <tbody>${sourceRows}</tbody></table></div>
      </div>
      <div class="card">
        <div class="card-head"><h2>Agents</h2><span class="muted small">from the Agent Manager</span></div>
        <div class="table-wrap"><table><thead><tr><th>Agent</th><th>Status</th></tr></thead>
          <tbody>${agentRows || '<tr><td colspan="2" class="muted">No agents registered.</td></tr>'}</tbody></table></div>
        <div class="card-head" style="margin-top:14px"><h2>Runs</h2></div>
        <div class="grid">
          ${statCard("Total", runs.total || 0)}
          ${statCard("Running", runs.running || 0)}
          ${statCard("Completed", runs.completed || 0)}
          ${statCard("Failed", runs.failed || 0)}
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-head"><h2>System health</h2>
        <span class="muted small">checked ${esc(fmtDate(health.checked_at))}</span></div>
      <div class="table-wrap"><table>
        <thead><tr><th>Check</th><th>Result</th><th>Detail</th></tr></thead>
        <tbody>${healthRows}</tbody></table></div>
      <p class="muted small" style="margin-top:10px">
        ${(health.providers && health.providers.available || []).map((p) => `<span class="tag">${esc(p)}</span>`).join(" ")}
        ${(health.providers && health.providers.unavailable || []).length
          ? `<br><span class="warn">Unavailable:</span> ` +
            (health.providers.unavailable).map((u) => `<span class="tag">${esc(u.provider)}: ${esc(u.reason)}</span>`).join(" ")
          : ""}
      </p>
    </div>

    <div class="card">
      <div class="card-head"><h2>Recent runs</h2></div>
      <div class="table-wrap"><table>
        <thead><tr><th>ID</th><th>Agent</th><th>Status</th><th>Summary</th><th>Duration</th></tr></thead>
        <tbody>${recentRuns}</tbody></table></div>
    </div>

    <div class="card">
      <div class="card-head"><h2>Recent errors</h2></div>
      <div class="table-wrap"><table>
        <thead><tr><th>When</th><th>Agent</th><th>Error</th></tr></thead>
        <tbody>${errors}</tbody></table></div>
    </div>
  `);
}

/* ================= AGENTS ================= */

async function renderAgents() {
  const body = $("#agentsBody");
  body.innerHTML = skeleton(3);
  let data;
  try {
    data = await api.get("/api/agents");
  } catch (err) {
    body.innerHTML = errorState(err.message);
    return;
  }

  const agents = data.agents || [];
  if (!agents.length) {
    body.innerHTML = emptyState("No agents registered", "The Agent Manager reported no agents.");
    return;
  }

  body.innerHTML = agents.map((a) => `
    <div class="card" data-agent="${esc(a.name)}">
      <div class="card-head">
        <h2>${esc(a.name)}</h2>
        ${badge(a.enabled ? "enabled" : "disabled", a.enabled ? "enabled" : "disabled")}
        <span class="muted small">${esc(a.description || "")}</span>
      </div>
      <div class="field-row">
        <button class="btn small" data-action="toggle" data-agent="${esc(a.name)}" data-enabled="${a.enabled}">
          ${a.enabled ? "Disable" : "Enable"}
        </button>
        <button class="btn small" data-action="configure" data-agent="${esc(a.name)}">Open configuration</button>
        <button class="btn small" data-action="run" data-agent="${esc(a.name)}">Run</button>
      </div>
      <details style="margin-top:12px">
        <summary class="muted small">Instructions &amp; settings</summary>
        <div style="margin-top:10px">
          ${kv([
            ["Instructions", `<span class="mono wrap-anywhere">${esc(a.instructions || "(none)")}</span>`],
            ["Allowed tools", (a.allowed_tools || []).map((t) => `<span class="tag">${esc(t)}</span>`).join(" ") || "—"],
            ["Settings", Object.keys(a.settings || {}).length
              ? `<span class="mono wrap-anywhere">${esc(JSON.stringify(a.settings))}</span>` : "defaults"],
            ["Credentials", "managed centrally — agents never hold secrets"],
          ])}
        </div>
      </details>
    </div>
  `).join("");
}

async function toggleAgent(name, enabled) {
  try {
    await api.put(`/api/agents/${encodeURIComponent(name)}`, { enabled: !enabled });
    toast(`${name} ${!enabled ? "enabled" : "disabled"}.`);
    renderAgents();
  } catch (err) { toast(err.message, true); }
}

async function openAgentConfiguration(name) {
  let agent;
  try {
    agent = await api.get(`/api/agents/${encodeURIComponent(name)}`);
  } catch (err) { toast(err.message, true); return; }

  const settingsSchema = agent.settings_schema || [];
  const current = agent.settings || {};

  const settingFields = settingsSchema.map((s) => {
    const value = current[s.name];
    if (s.type === "boolean") {
      return `<div class="field"><label class="checkbox">
          <input type="checkbox" data-setting="${esc(s.name)}" data-type="boolean"
            ${value === true ? "checked" : ""} /> ${esc(s.label)}
        </label>${s.help ? `<div class="help">${esc(s.help)}</div>` : ""}</div>`;
    }
    if (Array.isArray(s.choices)) {
      const opts = ["", ...s.choices].map((c) =>
        `<option value="${esc(c)}" ${String(value || "") === String(c) ? "selected" : ""}>${esc(c || "(default)")}</option>`
      ).join("");
      return `<div class="field"><label>${esc(s.label)}<select data-setting="${esc(s.name)}">${opts}</select></label>
        ${s.help ? `<div class="help">${esc(s.help)}</div>` : ""}</div>`;
    }
    const type = s.type === "integer" ? "number" : "text";
    const v = Array.isArray(value) ? value.join(",") : (value === undefined || value === null ? "" : value);
    return `<div class="field"><label>${esc(s.label)}
        <input type="${type}" data-setting="${esc(s.name)}" data-type="${esc(s.type)}" value="${esc(v)}" />
      </label>${s.help ? `<div class="help">${esc(s.help)}</div>` : ""}</div>`;
  }).join("") || `<p class="muted small">This agent declares no editable settings.</p>`;

  const tools = (agent.available_tools || []).map((t) => `
    <label class="checkbox"><input type="checkbox" data-tool="${esc(t)}"
      ${(agent.allowed_tools || []).includes(t) ? "checked" : ""} /> ${esc(t)}</label>`).join("")
    || `<p class="muted small">No tools declared for this agent.</p>`;

  openModal(`Configure ${name}`, `
    <div class="notice" style="margin-top:0">
      Credentials are not stored per agent. Manage provider keys under
      <strong>Credentials &amp; Providers</strong>.
    </div>
    <form id="agentForm" style="margin-top:14px">
      <div class="field">
        <label>System instructions
          <textarea data-field="instructions">${esc(agent.instructions || "")}</textarea>
        </label>
        <div class="help">What this agent is for. Shown to the operator; not executed as code.</div>
      </div>
      <div class="field">
        <label>Notes (optional)<input type="text" data-field="notes" value="${esc(agent.notes || "")}" /></label>
      </div>
      <h3>Settings</h3>
      ${settingFields}
      <h3>Allowed tools</h3>
      ${tools}
      <div class="field-row" style="margin-top:16px">
        <button class="btn primary" type="submit">Save configuration</button>
        <button class="btn" type="button" id="agentResetBtn">Reset to defaults</button>
      </div>
    </form>
  `);

  $("#agentForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const settings = {};
    $$("#modalBody [data-setting]").forEach((el) => {
      const key = el.getAttribute("data-setting");
      const type = el.getAttribute("data-type");
      if (type === "boolean") { settings[key] = el.checked; return; }
      const raw = el.value.trim();
      if (raw === "") return;
      if (type === "integer") settings[key] = parseInt(raw, 10);
      else if (type === "list") settings[key] = raw;
      else settings[key] = raw;
    });
    const allowed_tools = $$("#modalBody [data-tool]").filter((el) => el.checked)
      .map((el) => el.getAttribute("data-tool"));
    const payload = {
      instructions: $('#agentForm [data-field="instructions"]').value,
      notes: $('#agentForm [data-field="notes"]').value,
      settings,
      allowed_tools,
    };
    try {
      await api.put(`/api/agents/${encodeURIComponent(name)}`, payload);
      toast("Configuration saved.");
      closeModal();
      renderAgents();
    } catch (err) { toast(err.message, true); }
  });

  $("#agentResetBtn").addEventListener("click", async () => {
    try {
      await api.post(`/api/agents/${encodeURIComponent(name)}/reset`);
      toast("Agent reset to defaults.");
      closeModal();
      renderAgents();
    } catch (err) { toast(err.message, true); }
  });
}

async function runAgent(name) {
  const payload = name === "website_analyzer" ? { limit: 25 } : { limit: 25 };
  try {
    const run = await api.post(`/api/agents/${encodeURIComponent(name)}/run`, payload);
    toast(`Run ${run.id} started for ${name}. See Runs & Jobs.`);
    setTimeout(renderRuns, 400);
  } catch (err) { toast(err.message, true); }
}

/* ================= CREDENTIALS ================= */

async function renderCredentials() {
  const body = $("#credentialsBody");
  body.innerHTML = skeleton(4);
  let data;
  try {
    data = await api.get("/api/credentials");
  } catch (err) {
    body.innerHTML = errorState(err.message);
    return;
  }

  const groups = {};
  (data.providers || []).forEach((p) => { (groups[p.channel] = groups[p.channel] || []).push(p); });

  const labels = {
    search: "Search providers",
    llm: "LLM / API providers",
    email: "Email providers",
    messaging: "Messaging (WhatsApp Business API)",
  };

  body.innerHTML = Object.keys(groups).map((channel) => `
    <div class="card">
      <div class="card-head"><h2>${esc(labels[channel] || channel)}</h2></div>
      ${groups[channel].map((p) => {
        const secret = p.secret || {};
        const implemented = p.implemented !== false;
        return `<div style="border-top:1px solid var(--line); padding:14px 0">
          <div class="field-row">
            <strong>${esc(p.label)}</strong>
            ${p.requires_secret
              ? statusBadge(secret.configured, "configured")
              : badge("no key required", "good")}
            ${implemented ? "" : badge("reserved", "unknown")}
            <span class="muted small mono">${esc(p.key)}</span>
          </div>
          <p class="muted small" style="margin:8px 0">${esc(p.description || "")}</p>
          ${kv([
            ["Used by", `<span class="mono">${esc(p.used_by || "—")}</span>`],
            ["Env variable", p.secret_env ? `<span class="mono">${esc(p.secret_env)}</span>` : "—"],
            ["Stored value", !p.requires_secret
              ? `<span class="muted">not required</span>`
              : secret.configured
                ? `<span class="mask">${esc(secret.preview || "••••••••")}</span>
                   <span class="muted small"> (source: ${esc(secret.source || "unknown")})</span>`
                : `<span class="muted">not set</span>`],
            ["Settings", (p.settings || []).map((s) => `<span class="tag">${esc(s)}</span>`).join(" ") || "—"],
          ])}
          ${p.requires_secret ? `
          <div class="field-row" style="margin-top:10px">
            <input type="password" data-cred="${esc(p.key)}" placeholder="Paste a new value to store"
              autocomplete="new-password" spellcheck="false" style="max-width:320px" />
            <button class="btn small" data-action="save-cred" data-cred="${esc(p.key)}">Save</button>
            <button class="btn small danger" data-action="clear-cred" data-cred="${esc(p.key)}">Remove stored value</button>
          </div>
          <div class="help" style="margin-top:4px">
            Stored locally with owner-only permissions. Never shown again after saving.
          </div>` : ""}
        </div>`;
      }).join("")}
    </div>
  `).join("");
}

async function saveCredential(key) {
  const input = $(`#credentialsBody [data-cred="${CSS.escape(key)}"][type="password"]`);
  const value = input ? input.value.trim() : "";
  if (!value) { toast("Enter a value first.", true); return; }
  try {
    const state = await api.put(`/api/credentials/${encodeURIComponent(key)}`, { value });
    /* Clear immediately so the value does not linger in the DOM. */
    if (input) input.value = "";
    toast(state.configured
      ? `Saved. Preview: ${state.preview} (source: ${state.source})`
      : "Saved, but the environment still overrides it.");
    renderCredentials();
  } catch (err) { toast(err.message, true); }
}

async function clearCredential(key) {
  try {
    await api.del(`/api/credentials/${encodeURIComponent(key)}`);
    toast("Local value removed.");
    renderCredentials();
  } catch (err) { toast(err.message, true); }
}

/* ================= LEADS ================= */

let leadState = { limit: 25, offset: 0 };

async function loadFacets() {
  try {
    const f = await api.get("/api/leads/facets");
    const fill = (id, values, labelFn) => {
      const sel = $(id);
      const current = sel.value;
      sel.innerHTML = '<option value="">Any</option>' +
        (values || []).map((v) => `<option value="${esc(v)}">${esc(labelFn ? labelFn(v) : v)}</option>`).join("");
      sel.value = current;
    };
    fill("#fCity", f.cities);
    fill("#fCountry", f.countries);
    fill("#fType", f.business_types, humanise);
    fill("#fSource", f.sources);
    fill("#fPriority", f.priorities, humanise);
    fill("#fStatus", f.website_statuses, (s) => s.replace("website_", ""));
  } catch (err) { /* facets are a convenience; the table still works */ }
}

function leadQuery() {
  const form = $("#leadFilters");
  const data = new FormData(form);
  const params = new URLSearchParams();
  for (const [key, value] of data.entries()) {
    if (String(value).trim()) params.set(key, String(value).trim());
  }
  params.set("limit", String(leadState.limit));
  params.set("offset", String(leadState.offset));
  return params.toString();
}

async function renderLeads() {
  const body = $("#leadsBody");
  body.innerHTML = skeleton(6);
  let data;
  try {
    data = await api.get(`/api/leads?${leadQuery()}`);
  } catch (err) {
    body.innerHTML = errorState(err.message);
    return;
  }

  const leads = data.leads || [];
  if (!leads.length) {
    body.innerHTML = emptyState(
      "No leads match",
      data.total === 0
        ? "Run a search from Runs & Jobs, or relax the filters."
        : "Try clearing a filter."
    );
    return;
  }

  const rows = leads.map((l) => `<tr class="clickable" data-lead="${esc(l.id)}">
      <td>${esc(l.business_name)}</td>
      <td class="num">${esc(l.lead_score)}</td>
      <td>${priorityBadge(l.priority)}</td>
      <td>${websiteStatusBadge(l.website_status)}</td>
      <td>${esc(l.city || "—")}</td>
      <td>${esc(l.country || "—")}</td>
      <td>${(l.sources || []).map((s) => `<span class="tag">${esc(s)}</span>`).join(" ") || "—"}</td>
      <td>${esc(l.phone || "—")}</td>
    </tr>`).join("");

  const from = data.offset + 1;
  const to = Math.min(data.total, data.offset + leads.length);

  body.innerHTML = `
    <div class="table-wrap"><table>
      <thead><tr>
        <th>Business</th><th class="num">Score</th><th>Priority</th>
        <th>Website</th><th>City</th><th>Country</th><th>Source</th><th>Phone</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table></div>
    <div class="field-row" style="justify-content:space-between">
      <span class="muted small">Showing ${esc(from)}–${esc(to)} of ${esc(data.total)}${data.truncated ? " (scan capped)" : ""}</span>
      <span class="field-row">
        <button class="btn small" id="prevPage" ${data.offset === 0 ? "disabled" : ""}>Previous</button>
        <button class="btn small" id="nextPage" ${to >= data.total ? "disabled" : ""}>Next</button>
      </span>
    </div>`;

  const prev = $("#prevPage");
  const next = $("#nextPage");
  if (prev) prev.addEventListener("click", () => {
    leadState.offset = Math.max(0, leadState.offset - leadState.limit);
    renderLeads();
  });
  if (next) next.addEventListener("click", () => {
    leadState.offset += leadState.limit;
    renderLeads();
  });
  $$("#leadsBody tr[data-lead]").forEach((tr) => {
    tr.addEventListener("click", () => openLead(tr.getAttribute("data-lead")));
  });
}

async function openLead(id) {
  openModal("Lead detail", skeleton(4));
  let lead;
  try {
    lead = await api.get(`/api/leads/${encodeURIComponent(id)}`);
  } catch (err) {
    $("#modalBody").innerHTML = errorState(err.message);
    return;
  }

  const analysis = lead.website_analysis;
  const findings = (analysis && analysis.findings) || [];
  const findingsHtml = findings.length
    ? `<table><thead><tr><th>Severity</th><th>Finding</th><th>Detail</th></tr></thead><tbody>
        ${findings.map((f) => `<tr>
          <td><span class="sev ${esc(f.severity)}">${esc(f.severity)}</span></td>
          <td>${esc(f.kind)}</td><td class="wrap-anywhere">${esc(f.detail)}</td>
        </tr>`).join("")}</tbody></table>`
    : `<p class="muted small">No stored analysis for this lead. Run the Website Analyzer to produce one.</p>`;

  const breakdown = Object.entries(lead.score_breakdown || {})
    .map(([k, v]) => `<tr><td>${esc(humanise(k))}</td><td class="num">${esc(v)}</td></tr>`).join("");

  $("#modalTitle").textContent = lead.business_name || "Lead";
  $("#modalBody").innerHTML = `
    <div class="pill-row" style="margin-bottom:12px">
      ${priorityBadge(lead.priority)} ${websiteStatusBadge(lead.website_status)}
      ${badge("score " + lead.lead_score, "")} ${badge(lead.score_confidence + " confidence", "")}
    </div>
    ${kv([
      ["Business", esc(lead.business_name)],
      ["ID", `<span class="mono">${esc(lead.id)}</span>`],
      ["Type", esc(lead.business_type || "—")],
      ["Location", esc([lead.city, lead.country].filter(Boolean).join(", ") || "—")],
      ["Address", esc(lead.address || "—")],
      ["Phone", esc(lead.phone || "—")],
      ["Email", esc(lead.email || "—")],
      ["Website", lead.website_url
        ? `<span class="mono wrap-anywhere">${esc(lead.website_url)}</span>` : "—"],
      ["Website quality", esc(lead.website_quality || "—")],
      ["Business status", esc(lead.business_status || "—")],
      ["Sources", (lead.sources || []).map((s) => `<span class="tag">${esc(s)}</span>`).join(" ") || "—"],
      ["Discovered", esc(fmtDate(lead.discovered_at))],
      ["Last checked", esc(fmtDate(lead.last_checked_at))],
      ["Coordinates", lead.latitude && lead.longitude
        ? `<span class="mono">${esc(lead.latitude)}, ${esc(lead.longitude)}</span>` : "—"],
    ])}
    <h3 style="margin-top:18px">Scoring</h3>
    ${(lead.score_reason || []).length
      ? `<ul class="muted small">${lead.score_reason.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>`
      : `<p class="muted small">No scoring reasons recorded.</p>`}
    ${breakdown ? `<div class="table-wrap"><table><tbody>${breakdown}</tbody></table></div>` : ""}
    <h3 style="margin-top:18px">Website analysis</h3>
    ${analysis ? `<p class="muted small">Status ${esc(analysis.status)} ·
        ${analysis.needs_attention ? "needs attention" : "no action flagged"} ·
        ${analysis.from_stored_check ? "from a stored check" : "no stored check"}</p>` : ""}
    <div class="table-wrap">${findingsHtml}</div>
  `;
}

/* ================= RUNS ================= */

async function renderRuns() {
  const body = $("#runsBody");
  body.innerHTML = skeleton(5);
  let data;
  try {
    data = await api.get("/api/runs?limit=50");
  } catch (err) {
    body.innerHTML = errorState(err.message);
    return;
  }

  const counts = data.counts || {};
  const runs = data.runs || [];

  const rows = runs.map((r) => `<tr class="clickable" data-run="${esc(r.id)}">
      <td class="mono">${esc(r.id)}</td>
      <td>${esc(r.agent)}</td>
      <td>${badge(r.status, r.status === "completed" ? "good" : r.status === "failed" ? "bad" : "unknown")}</td>
      <td>${esc(r.summary || "—")}</td>
      <td>${esc(fmtDate(r.started_at))}</td>
      <td>${esc(r.duration_seconds !== null && r.duration_seconds !== undefined ? r.duration_seconds + "s" : "—")}</td>
    </tr>`).join("") || `<tr><td colspan="6" class="muted">No runs yet. Start one above.</td></tr>`;

  body.innerHTML = `
    <div class="card">
      <div class="card-head"><h2>Job counts</h2></div>
      <div class="grid">
        ${statCard("Total", counts.total || 0)}
        ${statCard("Running", counts.running || 0)}
        ${statCard("Completed", counts.completed || 0)}
        ${statCard("Failed", counts.failed || 0)}
      </div>
    </div>
    <div class="card">
      <div class="card-head"><h2>Execution history</h2>
        <span class="muted small">most recent first</span></div>
      <div class="table-wrap"><table>
        <thead><tr><th>ID</th><th>Agent</th><th>Status</th><th>Summary</th><th>Started</th><th>Duration</th></tr></thead>
        <tbody>${rows}</tbody></table></div>
    </div>`;

  $$("#runsBody tr[data-run]").forEach((tr) => {
    tr.addEventListener("click", () => openRun(tr.getAttribute("data-run")));
  });
}

async function openRun(id) {
  openModal("Run detail", skeleton(3));
  let run;
  try {
    run = await api.get(`/api/runs/${encodeURIComponent(id)}`);
  } catch (err) { $("#modalBody").innerHTML = errorState(err.message); return; }

  const metrics = run.metrics || {};
  const providerRows = (metrics.providers || []).map((p) => `<tr>
      <td>${esc(p.provider)}</td><td>${esc(p.kind || "—")}</td>
      <td class="num">${esc(p.count || 0)}</td>
      <td class="wrap-anywhere">${esc(p.error || p.skipped_reason || "—")}</td>
    </tr>`).join("");

  $("#modalTitle").textContent = `Run ${run.id}`;
  $("#modalBody").innerHTML = `
    <div class="pill-row" style="margin-bottom:12px">
      ${badge(run.status, run.status === "completed" ? "good" : run.status === "failed" ? "bad" : "unknown")}
      ${badge(run.agent, "")}
    </div>
    ${kv([
      ["Started", esc(fmtDate(run.started_at))],
      ["Finished", esc(fmtDate(run.finished_at))],
      ["Duration", esc(run.duration_seconds !== null && run.duration_seconds !== undefined ? run.duration_seconds + "s" : "—")],
      ["Summary", esc(run.summary || "—")],
      ["Error", run.error ? `<span class="warn wrap-anywhere">${esc(run.error)}</span>` : "—"],
      ["Parameters", `<span class="mono wrap-anywhere">${esc(JSON.stringify(run.params || {}))}</span>`],
    ])}
    ${metrics.raw_count !== undefined ? `
      <h3 style="margin-top:16px">Pipeline stages</h3>
      <div class="table-wrap"><table><tbody>
        <tr><td>Raw</td><td class="num">${esc(metrics.raw_count)}</td></tr>
        <tr><td>Normalized</td><td class="num">${esc(metrics.normalized_count)}</td></tr>
        <tr><td>Duplicates removed</td><td class="num">${esc(metrics.duplicates_removed)}</td></tr>
        <tr><td>Checked</td><td class="num">${esc(metrics.checked_count)}</td></tr>
        <tr><td>Scored</td><td class="num">${esc(metrics.scored_count)}</td></tr>
        <tr><td>Stored</td><td class="num">${esc(metrics.stored_count)}</td></tr>
        <tr><td>Elapsed</td><td class="num">${esc(metrics.elapsed_seconds)}s</td></tr>
        <tr><td>Failed stages</td><td>${(metrics.failed_stages || []).map((s) => `<span class="tag">${esc(s)}</span>`).join(" ") || "none"}</td></tr>
      </tbody></table></div>` : ""}
    ${providerRows ? `<h3 style="margin-top:16px">Providers</h3>
      <div class="table-wrap"><table><thead><tr><th>Provider</th><th>Kind</th><th class="num">Results</th><th>Note</th></tr></thead>
      <tbody>${providerRows}</tbody></table></div>` : ""}
  `;

  if (run.status === "running") setTimeout(() => { if (!$("#modalBackdrop").hidden) openRun(id); }, 1500);
}

/* ================= SETTINGS ================= */

async function renderSettings() {
  const body = $("#settingsBody");
  body.innerHTML = skeleton(5);
  let data;
  try {
    data = await api.get("/api/settings");
  } catch (err) { body.innerHTML = errorState(err.message); return; }

  const settings = data.settings || {};
  const groups = {};
  Object.entries(settings).forEach(([name, meta]) => {
    const g = meta.group || "General";
    (groups[g] = groups[g] || []).push([name, meta]);
  });

  body.innerHTML = Object.keys(groups).sort().map((group) => `
    <div class="card">
      <div class="card-head"><h2>${esc(group)}</h2></div>
      ${groups[group].map(([name, meta]) => {
        const value = meta.value;
        const display = Array.isArray(value) ? value.join(",") : (value === null || value === undefined ? "" : value);
        const badgeHtml = meta.source === "override"
          ? badge("overridden", "warm") : badge("default", "unknown");
        const type = meta.type === "int" || meta.type === "float" ? "number" : "text";
        const input = meta.type === "bool"
          ? `<label class="checkbox"><input type="checkbox" data-setting="${esc(name)}" data-type="bool"
               ${value ? "checked" : ""} /> ${esc(meta.label || name)}</label>`
          : `<label>${esc(meta.label || name)}
               <input type="${type}" data-setting="${esc(name)}" data-type="${esc(meta.type)}"
                 value="${esc(display)}" /></label>`;
        return `<div style="border-top:1px solid var(--line); padding:12px 0">
          <div class="field-row">${input} ${badgeHtml}</div>
          <div class="help mono">${esc(name)}</div>
        </div>`;
      }).join("")}
    </div>
  `).join("") + `
    <div class="field-row">
      <button class="btn primary" id="saveSettings">Save settings</button>
      <span class="muted small">A cleared field restores the underlying default.</span>
    </div>`;

  $("#saveSettings").addEventListener("click", async () => {
    const values = {};
    $$("#settingsBody [data-setting]").forEach((el) => {
      const key = el.getAttribute("data-setting");
      const type = el.getAttribute("data-type");
      if (type === "bool") { values[key] = el.checked; return; }
      values[key] = el.value.trim() === "" ? null : el.value.trim();
    });
    try {
      await api.put("/api/settings", { values });
      toast("Settings saved.");
      renderSettings();
    } catch (err) { toast(err.message, true); }
  });
}

/* ================= navigation ================= */

const VIEWS = {
  overview: { panel: "#view-overview", render: renderOverview },
  agents: { panel: "#view-agents", render: renderAgents },
  credentials: { panel: "#view-credentials", render: renderCredentials },
  leads: { panel: "#view-leads", render: async () => { await loadFacets(); return renderLeads(); } },
  runs: { panel: "#view-runs", render: renderRuns },
  settings: { panel: "#view-settings", render: renderSettings },
};

function show(view) {
  if (!VIEWS[view]) view = "overview";
  Object.entries(VIEWS).forEach(([name, cfg]) => { $(cfg.panel).hidden = name !== view; });
  $$(".nav-item").forEach((btn) => {
    btn.setAttribute("aria-selected", String(btn.getAttribute("data-view") === view));
  });
  $("#sidebar").classList.remove("open");
  $("#navToggle").setAttribute("aria-expanded", "false");
  if (location.hash !== `#${view}`) history.replaceState(null, "", `#${view}`);
  VIEWS[view].render();
}

async function refreshHealth() {
  try {
    const h = await api.get("/api/health");
    const pill = $("#healthPill");
    pill.classList.toggle("ok", !!h.ok);
    pill.classList.toggle("bad", !h.ok);
    $("#healthText").textContent = h.ok ? "healthy" : "attention needed";
  } catch (err) {
    $("#healthPill").classList.add("bad");
    $("#healthText").textContent = "unreachable";
  }
}

/* ================= wiring ================= */

document.addEventListener("DOMContentLoaded", async () => {
  try {
    const index = await api.get("/api");
    $("#versionText").textContent = `v${index.version}`;
  } catch (err) { /* non-fatal */ }

  $$(".nav-item").forEach((btn) => {
    btn.addEventListener("click", () => show(btn.getAttribute("data-view")));
  });
  $("#navToggle").addEventListener("click", () => {
    const open = $("#sidebar").classList.toggle("open");
    $("#navToggle").setAttribute("aria-expanded", String(open));
  });
  $("#modalClose").addEventListener("click", closeModal);
  $("#modalBackdrop").addEventListener("click", (ev) => {
    if (ev.target === $("#modalBackdrop")) closeModal();
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && !$("#modalBackdrop").hidden) closeModal();
  });
  // Deep links and browser back/forward change only the hash, which does not
  // reload the page. Without this, the URL and the visible section disagree.
  // `show()` uses replaceState, so it cannot re-trigger this handler.
  window.addEventListener("hashchange", () => {
    const name = (location.hash || "#overview").slice(1);
    if (VIEWS[name]) show(name);
  });

  $("#refreshOverview").addEventListener("click", renderOverview);
  $("#refreshLeads").addEventListener("click", renderLeads);
  $("#refreshRuns").addEventListener("click", renderRuns);
  $("#runSearchBtn").addEventListener("click", async () => {
    try {
      const run = await api.post("/api/runs/search", { limit: 25 });
      toast(`Search run ${run.id} started.`);
      renderRuns();
    } catch (err) { toast(err.message, true); }
  });
  $("#runAnalyzeBtn").addEventListener("click", async () => {
    try {
      const run = await api.post("/api/runs/analyze", { limit: 25 });
      toast(`Analysis run ${run.id} started.`);
      renderRuns();
    } catch (err) { toast(err.message, true); }
  });

  $("#leadFilters").addEventListener("submit", (ev) => {
    ev.preventDefault();
    leadState.offset = 0;
    renderLeads();
  });
  $("#leadFilters").addEventListener("reset", () => {
    setTimeout(() => { leadState.offset = 0; renderLeads(); }, 0);
  });

  $("#agentsBody").addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-action]");
    if (!btn) return;
    const name = btn.getAttribute("data-agent");
    const action = btn.getAttribute("data-action");
    if (action === "toggle") toggleAgent(name, btn.getAttribute("data-enabled") === "true");
    else if (action === "configure") openAgentConfiguration(name);
    else if (action === "run") runAgent(name);
  });

  $("#credentialsBody").addEventListener("click", (ev) => {
    const btn = ev.target.closest("[data-action]");
    if (!btn) return;
    const key = btn.getAttribute("data-cred");
    if (btn.getAttribute("data-action") === "save-cred") saveCredential(key);
    else if (btn.getAttribute("data-action") === "clear-cred") clearCredential(key);
  });

  const initial = (location.hash || "#overview").slice(1);
  show(initial in VIEWS ? initial : "overview");
  refreshHealth();
  setInterval(refreshHealth, 30000);
});