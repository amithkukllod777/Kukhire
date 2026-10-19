/* KukHire web UI. Plain JS, no build step. Rules this file keeps:
 *  - every piece of user/candidate data reaches the page through textContent, never innerHTML;
 *  - there is no password handling of our own: sign-in calls the shared Kuklabs AuthKit REST
 *    contract on the configured identity server, and the API verifies the token via the same contract;
 *  - what a role can do comes from the server's capability list (the server enforces it regardless). */
"use strict";

const STAGES = ["applied", "screening", "interview", "offer", "hired"];
const CLOSED = ["rejected", "withdrawn"];
const STAGE_LABEL = { applied: "Applied", screening: "Screening", interview: "Interview", offer: "Offer", hired: "Hired", rejected: "Rejected", withdrawn: "Withdrawn" };

const S = { cfg: null, session: null, me: null, orgId: null, org: null, view: "pipeline", jobFilter: "", pendingOtp: null, data: {} };
const root = document.getElementById("root");

/* ---------- helpers ---------- */
function el(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "value") node.value = v;
    else if (k in node && k !== "list") node[k] = v;
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return node;
}
function toast(message, isError) {
  const t = el("div", { class: "toast" + (isError ? " err" : "") }, message);
  document.getElementById("toasts").append(t);
  setTimeout(() => t.remove(), 5000);
}
function field(label, input) { return el("div", {}, el("label", {}, label), input); }
function can(cap) { return !!S.org && S.org.capabilities.includes(cap); }

class ApiError extends Error {
  constructor(status, body) {
    const d = body && body.detail;
    super(typeof d === "object" && d && !Array.isArray(d) ? d.message || d.error : Array.isArray(d) ? d.map((x) => x.msg).join("; ") : "Request failed");
    this.status = status; this.code = d && d.error; this.detail = d;
  }
}

/* ---------- identity (shared Kuklabs Account, AuthKit REST contract) ---------- */
const store = { get() { try { return JSON.parse(sessionStorage.getItem("kukhire.session")); } catch { return null; } },
  set(v) { try { v ? sessionStorage.setItem("kukhire.session", JSON.stringify(v)) : sessionStorage.removeItem("kukhire.session"); } catch { /* storage blocked: stay in-memory */ } } };

async function authCall(path, body, token) {
  const res = await fetch(S.cfg.auth_base_url + path, { method: "POST",
    headers: { "Content-Type": "application/json", "X-Kuklabs-Product": S.cfg.product, ...(token ? { Authorization: "Bearer " + token } : {}) },
    body: JSON.stringify(body || {}) });
  return { status: res.status, body: await res.json().catch(() => ({})) };
}
function adopt(body) { S.session = { access_token: body.access_token, refresh_token: body.refresh_token }; store.set(S.session); }
/* Refresh tokens ROTATE (each is single-use), and a screen load fires several requests at once.
 * If each 401 started its own refresh, the first would win and the rest would fail with a spent
 * token and sign the user out. So concurrent callers share ONE refresh. */
let refreshing = null;
function refreshSession() {
  if (!S.session || !S.session.refresh_token) return Promise.resolve(false);
  if (!refreshing) {
    refreshing = authCall("/v1/auth/token/refresh", { refresh_token: S.session.refresh_token })
      .then((r) => { if (r.status === 200 && r.body.access_token) { adopt(r.body); return true; } return false; })
      .catch(() => false)
      .finally(() => { refreshing = null; });
  }
  return refreshing;
}
async function api(method, path, body, retried) {
  const res = await fetch(path, { method, headers: { "Content-Type": "application/json", ...(S.session ? { Authorization: "Bearer " + S.session.access_token } : {}) },
    body: body === undefined ? undefined : JSON.stringify(body) });
  if (res.status === 401 && !retried && await refreshSession()) return api(method, path, body, true);
  if (res.status === 401) { signOutLocal(); throw new ApiError(401, { detail: { message: "Session expired" } }); }
  if (res.status === 204) return null;
  const json = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, json);
  return json;
}
function signOutLocal() { S.session = null; S.me = null; S.org = null; store.set(null); render(); }
async function signOut() { try { if (S.session) await authCall("/v1/auth/logout", { refresh_token: S.session.refresh_token }, S.session.access_token); } catch { /* local sign-out regardless */ } signOutLocal(); }

/* ---------- screens ---------- */
function renderLogin() {
  const err = el("div", { class: "error", role: "alert" });
  const idInput = el("input", { id: "identifier", type: "text", autocomplete: "username", placeholder: "Email or mobile", value: S.pendingOtp ? S.pendingOtp.identifier : "" });
  const pw = el("input", { id: "password", type: "password", autocomplete: "current-password" });
  const code = el("input", { id: "otp", type: "text", inputMode: "numeric", autocomplete: "one-time-code", placeholder: "6-digit code" });
  const form = el("form", { onsubmit: async (e) => {
    e.preventDefault(); err.textContent = "";
    try {
      const r = S.pendingOtp ? await authCall("/v1/auth/otp/verify", { identifier: S.pendingOtp.identifier, code: code.value.trim() })
                             : await authCall("/v1/auth/login", { identifier: idInput.value.trim(), password: pw.value });
      if (r.status === 200 && r.body.access_token) { S.pendingOtp = null; adopt(r.body); await boot(); return; }
      if (r.status === 403 && (r.body.status === "otp_required")) { S.pendingOtp = { identifier: idInput.value.trim() || S.pendingOtp.identifier }; render(); return; }
      err.textContent = r.body.message || "Sign-in failed.";
    } catch { err.textContent = "Could not reach the sign-in service."; }
  } },
    S.pendingOtp ? [el("p", { class: "muted" }, "Enter the code we sent you."), field("Verification code", code)]
                 : [field("Email or mobile", idInput), field("Password", pw)],
    el("div", { class: "row", style: "margin-top:16px" }, el("button", { class: "btn primary", type: "submit" }, S.pendingOtp ? "Verify" : "Sign in")),
    err);
  root.replaceChildren(el("div", { class: "login card" },
    el("div", { class: "brand" }, "KukHire"), el("p", { class: "muted" }, "Sign in with your Kuklabs Account."),
    S.cfg.auth_base_url ? form : el("p", { class: "error", role: "alert" }, "Sign-in is not configured for this deployment."),
    el("p", { class: "muted small" }, "Powered by Kuklabs")));
}

function renderWorkspacePicker() {
  const name = el("input", { id: "org-name", placeholder: "Workspace name (e.g. Acme Hiring)" });
  const list = S.me.organizations.length
    ? S.me.organizations.map((o) => el("button", { class: "btn", style: "display:block;width:100%;margin-bottom:8px;text-align:left", onclick: () => openOrg(o.id) }, o.name, " ", el("span", { class: "badge" }, o.role)))
    : el("p", { class: "muted" }, "You are not in a workspace yet. Create one, or ask an admin to invite your email.");
  root.replaceChildren(el("div", { class: "login card" }, el("h1", {}, "Choose a workspace"), list,
    el("h3", {}, "New workspace"), name,
    el("div", { style: "margin-top:12px" }, el("button", { class: "btn primary", id: "create-org", onclick: async () => {
      try { const o = await api("POST", "/v1/organizations", { name: name.value.trim() }); S.me.organizations.push(o); await openOrg(o.id); }
      catch (e) { toast(e.message, true); } } }, "Create workspace")),
    el("p", { style: "margin-top:16px" }, el("button", { class: "btn small", onclick: signOut }, "Sign out"))));
}

async function openOrg(id) {
  S.orgId = id; try { localStorage.setItem("kukhire.org", id); } catch { /* optional convenience */ }
  S.org = await api("GET", "/v1/organizations/" + id);
  S.view = "pipeline"; S.data = {};
  await load(); render();
}
const base = () => "/v1/organizations/" + S.orgId;

async function load() {
  const [jobs, apps, stats] = await Promise.all([api("GET", base() + "/jobs"), api("GET", base() + "/applications?limit=200" + (S.jobFilter ? "&job_id=" + S.jobFilter : "")), api("GET", base() + "/stats")]);
  S.data.jobs = jobs.items; S.data.apps = apps.items; S.data.stats = stats;
}

function shell(content) {
  const items = [["pipeline", "Pipeline"], ["jobs", "Jobs"], ["candidates", "Candidates"], ...(can("org.manage") ? [["settings", "Settings"]] : [])];
  const nav = (cls) => items.map(([v, label]) => el("button", { class: "navbtn", "data-nav": v, "aria-current": S.view === v ? "page" : null, onclick: async () => { S.view = v; await load(); render(); } }, label));
  root.replaceChildren(el("div", { class: "shell" },
    el("header", { class: "topbar" }, el("span", { class: "brand" }, "KukHire"),
      el("button", { class: "btn small", id: "switch-org", onclick: () => { S.org = null; S.orgId = null; render(); } }, S.org.name, " ▾"),
      el("span", { class: "badge", id: "role-badge" }, S.org.role), el("span", { class: "spacer" }),
      el("span", { class: "muted small" }, S.me.user.name || S.me.user.email), el("button", { class: "btn small", onclick: signOut }, "Sign out")),
    el("nav", { class: "sidebar", "aria-label": "Main" }, nav(), el("div", { class: "foot" }, "Powered by Kuklabs", el("br"), "Version ", S.cfg.version)),
    el("main", { class: "main", id: "main" }, content),
    el("nav", { class: "bottomnav", "aria-label": "Main (mobile)" }, nav())));
}

function stageBadge(stage) { return el("span", { class: "badge " + (stage === "hired" ? "success" : stage === "rejected" ? "danger" : stage === "withdrawn" ? "warning" : "info") }, STAGE_LABEL[stage] || stage); }

/* --- pipeline --- */
function viewPipeline() {
  const jobSel = el("select", { id: "job-filter", onchange: async (e) => { S.jobFilter = e.target.value; await load(); render(); } },
    el("option", { value: "" }, "All jobs"), S.data.jobs.map((j) => el("option", { value: j.id, selected: j.id === S.jobFilter }, j.title)));
  const apps = S.data.apps;
  const col = (title, stages) => {
    const items = apps.filter((a) => stages.includes(a.stage));
    return el("section", { class: "col", "data-col": stages[0], "aria-label": title },
      el("h3", {}, title, el("span", { class: "muted" }, items.length)),
      items.map((a) => el("button", { class: "appcard", "data-app": a.id, onclick: () => openApplication(a.id) },
        el("strong", {}, a.candidate_name), el("div", { class: "muted small" }, a.job_title),
        el("div", { class: "small" }, stages.length > 1 ? stageBadge(a.stage) : null, a.rating ? " " + "★".repeat(a.rating) : ""))));
  };
  const s = S.data.stats;
  return [el("h1", {}, "Pipeline"),
    el("p", { class: "muted" }, `${s.open_jobs} open jobs · ${s.open_applications} open applications · ${s.candidates} candidates`),
    el("div", { class: "row", style: "max-width:320px;margin-bottom:12px" }, jobSel),
    el("div", { class: "board" }, STAGES.map((st) => col(STAGE_LABEL[st], [st])), col("Closed", CLOSED))];
}

/* --- jobs --- */
function viewJobs() {
  const rows = S.data.jobs.map((j) => el("tr", { "data-job": j.id },
    el("td", {}, el("strong", {}, j.title), el("div", { class: "muted small" }, [j.department, j.location].filter(Boolean).join(" · "))),
    el("td", {}, can("jobs.write") ? el("select", { "aria-label": "Status of " + j.title, onchange: async (e) => {
      try { await api("PATCH", base() + "/jobs/" + j.id, { status: e.target.value }); toast("Job updated"); await load(); render(); } catch (err) { toast(err.message, true); } } },
      ["draft", "open", "on_hold", "closed"].map((v) => el("option", { value: v, selected: v === j.status }, v))) : j.status),
    el("td", {}, j.applications_count), el("td", { class: "muted small" }, (j.scoring_criteria || []).map((c) => c.label).join(", ") || "—")));
  return [el("h1", {}, "Jobs"),
    can("jobs.write") ? jobForm() : null,
    el("div", { class: "card" }, el("table", {}, el("thead", {}, el("tr", {}, ["Job", "Status", "Applications", "Scoring criteria"].map((h) => el("th", {}, h)))), el("tbody", {}, rows)))];
}
function jobForm() {
  const f = { title: el("input", { id: "job-title" }), description: el("textarea", { id: "job-description" }), department: el("input", { id: "job-department" }), location: el("input", { id: "job-location" }),
    type: el("select", { id: "job-type" }, ["full_time", "part_time", "contract", "internship"].map((v) => el("option", { value: v }, v))),
    openings: el("input", { id: "job-openings", type: "number", min: 1, value: 1 }), status: el("select", { id: "job-status" }, ["draft", "open"].map((v) => el("option", { value: v }, v))) };
  const crit = el("div", { id: "criteria" });
  const addRow = (c = {}) => crit.append(el("div", { class: "criteria-row" },
    el("input", { placeholder: "key (e.g. python)", "data-f": "key", value: c.key || "" }), el("input", { placeholder: "Label", "data-f": "label", value: c.label || "" }),
    el("input", { placeholder: "Weight", type: "number", min: 1, max: 100, "data-f": "weight", value: c.weight || "" }), el("input", { placeholder: "What good looks like", "data-f": "description", value: c.description || "" }),
    el("button", { class: "btn small", type: "button", onclick: (e) => e.target.closest(".criteria-row").remove() }, "✕")));
  addRow();
  return el("details", { class: "card" }, el("summary", {}, "New job"),
    el("form", { id: "job-form", onsubmit: async (e) => {
      e.preventDefault();
      const criteria = [...crit.querySelectorAll(".criteria-row")].map((r) => Object.fromEntries([...r.querySelectorAll("input")].map((i) => [i.dataset.f, i.dataset.f === "weight" ? Number(i.value) : i.value]))).filter((c) => c.key || c.label);
      try {
        await api("POST", base() + "/jobs", { title: f.title.value, description: f.description.value, department: f.department.value || null, location: f.location.value || null,
          employment_type: f.type.value, openings: Number(f.openings.value), status: f.status.value, scoring_criteria: criteria.length ? criteria : null });
        toast("Job created"); await load(); render();
      } catch (err) { toast(err.message, true); } } },
      field("Title", f.title), field("Description", f.description),
      el("div", { class: "row" }, field("Department", f.department), field("Location", f.location), field("Type", f.type), field("Openings", f.openings), field("Status", f.status)),
      el("h3", {}, "Scoring criteria (used by the optional advisory AI evaluation)"), crit,
      el("button", { class: "btn small", type: "button", onclick: () => addRow() }, "+ Add criterion"),
      el("div", { style: "margin-top:12px" }, el("button", { class: "btn primary", type: "submit" }, "Create job"))));
}

/* --- candidates / new application --- */
async function viewCandidates() {
  const q = S.data.q || "";
  const list = await api("GET", base() + "/candidates" + (q ? "?q=" + encodeURIComponent(q) : ""));
  const search = el("input", { id: "candidate-search", placeholder: "Search name or email", value: q, onkeydown: async (e) => { if (e.key === "Enter") { S.data.q = e.target.value; render(); } } });
  return [el("h1", {}, "Candidates"), can("applications.write") ? applicationForm() : null,
    el("div", { class: "row", style: "max-width:360px;margin-bottom:12px" }, search),
    el("div", { class: "card" }, el("table", {}, el("thead", {}, el("tr", {}, ["Name", "Email", "Source", "Added"].map((h) => el("th", {}, h)))),
      el("tbody", {}, list.items.map((c) => el("tr", { "data-candidate": c.id }, el("td", {}, c.full_name), el("td", {}, c.email || "—"), el("td", {}, c.source || "—"), el("td", { class: "muted small" }, (c.created_at || "").slice(0, 10)))))),
      list.items.length ? null : el("p", { class: "muted" }, "No candidates found."))];
}
function applicationForm() {
  const f = { job: el("select", { id: "app-job" }, S.data.jobs.filter((j) => j.status !== "closed").map((j) => el("option", { value: j.id }, j.title))),
    name: el("input", { id: "app-name" }), email: el("input", { id: "app-email", type: "email" }), phone: el("input", { id: "app-phone" }),
    source: el("select", { id: "app-source" }, ["manual", "referral", "linkedin", "website", "agency", "other"].map((v) => el("option", { value: v }, v))),
    referrer: el("input", { id: "app-referrer", placeholder: "Referrer employee id or name (referrals)" }),
    salary: el("input", { id: "app-salary", type: "number", min: 0 }), notice: el("input", { id: "app-notice" }), notes: el("textarea", { id: "app-notes" }) };
  return el("details", { class: "card" }, el("summary", {}, "Add application"), el("form", { id: "app-form", onsubmit: async (e) => {
    e.preventDefault();
    const ref = f.referrer.value.trim();
    const body = { job_id: f.job.value, candidate: { full_name: f.name.value, email: f.email.value || null, phone: f.phone.value || null }, source: f.source.value,
      expected_salary: f.salary.value ? Number(f.salary.value) : null, notice_period: f.notice.value || null, notes: f.notes.value || null };
    if (f.source.value === "referral") { if (/^\d+$/.test(ref)) body.referrer_employee_ref = ref; else body.referrer_name = ref; }
    try { await api("POST", base() + "/applications", body); toast("Application added"); await load(); render(); } catch (err) { toast(err.message, true); } } },
    el("div", { class: "row" }, field("Job", f.job), field("Full name", f.name), field("Email", f.email), field("Phone", f.phone)),
    el("div", { class: "row" }, field("Source", f.source), field("Referrer", f.referrer), field("Expected salary", f.salary), field("Notice period", f.notice)),
    field("Notes", f.notes), el("div", { style: "margin-top:12px" }, el("button", { class: "btn primary", type: "submit" }, "Add application"))));
}

/* --- application drawer --- */
async function openApplication(id) {
  let a; try { a = await api("GET", base() + "/applications/" + id); } catch (e) { toast(e.message, true); return; }
  let scorecards = []; try { scorecards = (await api("GET", base() + "/applications/" + id + "/scorecards")).items; } catch { /* role may not read scorecards */ }
  const close = () => { document.querySelector(".drawer-backdrop")?.remove(); document.removeEventListener("keydown", onKey); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  const refresh = async () => { close(); await load(); render(); await openApplication(id); };

  // Offer the next forward stage first (the common case), then the rest.
  const order = [...a.allowed_transitions].sort((x, y) => {
    const here = STAGES.indexOf(a.stage), rank = (st) => { const i = STAGES.indexOf(st); return i > here ? i - here : i < 0 ? 50 : 100 + i; };
    return rank(x) - rank(y);
  });
  const target = el("select", { id: "move-to" }, order.map((s) => el("option", { value: s }, STAGE_LABEL[s])));
  const reason = el("input", { id: "move-reason", placeholder: "Reason (required to reopen)" });
  const move = a.allowed_transitions.length ? el("div", { class: "card" }, el("h2", {}, "Move stage"), el("div", { class: "row" }, target, reason),
    el("button", { class: "btn primary", id: "move-btn", style: "margin-top:8px", onclick: async () => {
      try { await api("POST", base() + "/applications/" + id + "/transitions", { to_stage: target.value, reason: reason.value || null }); toast("Moved to " + STAGE_LABEL[target.value]); await refresh(); }
      catch (e) { toast(e.message, true); } } }, "Move")) : el("p", { class: "muted" }, a.stage === "hired" ? "Hired applications are final in recruitment." : null);

  const rating = el("select", { id: "d-rating" }, el("option", { value: "" }, "—"), [1, 2, 3, 4, 5].map((n) => el("option", { value: n, selected: a.rating === n }, "★".repeat(n))));
  const assigned = el("input", { id: "d-assigned", value: a.assigned_to || "" });
  const notes = el("textarea", { id: "d-notes", value: a.notes || "" });
  const interview = el("input", { id: "d-interview", type: "date", value: a.interview_date || "" });
  const details = can("applications.write") ? el("div", { class: "card" }, el("h2", {}, "Details"),
    el("div", { class: "row" }, field("Rating", rating), field("Assigned to", assigned), field("Interview date", interview)), field("Notes", notes),
    el("button", { class: "btn", id: "save-details", style: "margin-top:8px", onclick: async () => {
      try { await api("PATCH", base() + "/applications/" + id, { rating: rating.value ? Number(rating.value) : null, assigned_to: assigned.value || null, notes: notes.value || null, interview_date: interview.value || null }); toast("Saved"); await refresh(); }
      catch (e) { toast(e.message, true); } } }, "Save")) : el("div", { class: "card" }, el("h2", {}, "Details"), el("p", {}, a.notes || "No notes."));

  const sc = el("div", { class: "card" }, el("h2", {}, "Scorecards"),
    scorecards.length ? scorecards.map((s) => el("p", { "data-scorecard": s.id }, el("strong", {}, `${s.overall}/5`), ` ${s.interviewer_name || "Interviewer"}${s.mine ? " (you)" : ""} — ${s.notes || ""}`)) : el("p", { class: "muted" }, "No scorecards visible to you yet."),
    can("scorecards.write") && !["rejected", "withdrawn"].includes(a.stage) ? scorecardForm(id, refresh) : null);

  const handoff = el("div", { class: "card" }, el("h2", {}, "Hand-off to HRMS"),
    a.handoff ? el("p", { id: "handoff-status" }, el("span", { class: "badge " + (a.handoff.status === "completed" ? "success" : "warning") }, a.handoff.status),
      a.handoff.employee_ref ? ` Employee ${a.handoff.employee_code || ""} (ref ${a.handoff.employee_ref})` : "", a.handoff.last_error ? " — " + a.handoff.last_error : "") : el("p", { class: "muted" }, "Not handed off."),
    a.stage === "hired" && can("handoff.request") && !(a.handoff && a.handoff.status === "completed") ? handoffForm(id, refresh) : null,
    a.stage !== "hired" ? el("p", { class: "muted small" }, "Available once the application is Hired. HRMS creates the employee and runs onboarding.") : null);

  const evalBox = can("evaluation.run") ? evaluationBox(a, id, refresh) : null;
  const hist = el("div", { class: "card" }, el("h2", {}, "History"), el("ul", { class: "timeline" },
    a.history.map((h) => el("li", {}, `${h.from_stage ? STAGE_LABEL[h.from_stage] + " → " : ""}${STAGE_LABEL[h.to_stage] || h.to_stage}`, " ", el("span", { class: "muted small" }, (h.created_at || "").slice(0, 16).replace("T", " ") + (h.reason ? " · " + h.reason : ""))))));

  const drawer = el("aside", { class: "drawer", role: "dialog", "aria-label": "Application: " + a.candidate_name, id: "drawer" },
    el("div", { class: "row", style: "align-items:center" }, el("h1", {}, a.candidate_name), el("button", { class: "btn small", id: "close-drawer", onclick: close, style: "flex:0 0 auto" }, "Close")),
    el("p", { class: "muted" }, [a.candidate_email, a.job_title].filter(Boolean).join(" · "), " ", stageBadge(a.stage)),
    a.source ? el("p", { class: "small" }, "Source: " + a.source + (a.referrer_name || a.referrer_employee_ref ? " · referred by " + (a.referrer_name || "employee " + a.referrer_employee_ref) : "")) : null,
    move, details, sc, handoff, evalBox, hist);
  const backdrop = el("div", { class: "drawer-backdrop", onclick: (e) => { if (e.target === backdrop) close(); } }, drawer);
  document.addEventListener("keydown", onKey);
  document.body.append(backdrop);
  document.getElementById("close-drawer").focus();
}
function scorecardForm(id, refresh) {
  const o = el("select", { id: "sc-overall" }, [5, 4.5, 4, 3.5, 3, 2.5, 2, 1.5, 1].map((n) => el("option", { value: n }, n)));
  const t = el("select", { id: "sc-tech" }, el("option", { value: "" }, "—"), [5, 4, 3, 2, 1].map((n) => el("option", { value: n }, n)));
  const c = el("select", { id: "sc-culture" }, el("option", { value: "" }, "—"), [5, 4, 3, 2, 1].map((n) => el("option", { value: n }, n)));
  const n = el("textarea", { id: "sc-notes", placeholder: "Evidence-based feedback" });
  return el("form", { id: "scorecard-form", onsubmit: async (e) => { e.preventDefault();
    try { await api("POST", base() + "/applications/" + id + "/scorecards", { overall: Number(o.value), technical: t.value ? Number(t.value) : null, culture: c.value ? Number(c.value) : null, notes: n.value || null }); toast("Scorecard saved"); await refresh(); }
    catch (err) { toast(err.message, true); } } },
    el("div", { class: "row" }, field("Overall", o), field("Technical", t), field("Culture", c)), field("Notes", n),
    el("button", { class: "btn", type: "submit", style: "margin-top:8px" }, "Add scorecard"));
}
function handoffForm(id, refresh) {
  const d = el("input", { id: "handoff-start", type: "date" });
  return el("div", {}, field("Start date (optional)", d), el("button", { class: "btn primary", id: "handoff-btn", style: "margin-top:8px", onclick: async () => {
    try { await api("POST", base() + "/applications/" + id + "/handoff", { start_date: d.value || null }); toast("HRMS confirmed the hire"); await refresh(); }
    catch (e) { toast(e.message, true); await refresh(); } } }, "Hand off to HRMS"));
}
function evaluationBox(a, id, refresh) {
  const e = a.latest_evaluation;
  return el("div", { class: "card" }, el("h2", {}, "Advisory evaluation"),
    el("p", { class: "muted small" }, "AI output is advisory. It never changes a stage, rejects or hires — a person decides."),
    e && e.status === "completed" ? el("div", { id: "eval-result" }, el("p", {}, el("strong", {}, "Weighted overall " + e.overall + " / 10"), " · " + e.model),
      e.criteria.map((c) => el("p", {}, el("strong", {}, c.key + ": " + c.score + "/10"), " — " + c.evidence)), e.summary ? el("p", {}, e.summary) : null) : e ? el("p", { class: "muted" }, "Last attempt failed: " + e.error) : el("p", { class: "muted" }, "No evaluation yet."),
    el("button", { class: "btn", id: "eval-btn", onclick: async () => {
      try { await api("POST", base() + "/applications/" + id + "/evaluations"); toast("Evaluation recorded"); await refresh(); }
      catch (err) { toast(err.message, true); } } }, "Run advisory evaluation"));
}

/* --- settings (admin) --- */
async function viewSettings() {
  const [members, hrms, audit] = await Promise.all([api("GET", base() + "/members"), api("GET", base() + "/integrations/hrms"), api("GET", base() + "/audit")]);
  const email = el("input", { id: "invite-email", type: "email", placeholder: "colleague@company.com" });
  const role = el("select", { id: "invite-role" }, ["recruiter", "interviewer", "viewer", "admin"].map((r) => el("option", { value: r }, r)));
  const url = el("input", { id: "hrms-url", placeholder: "https://hrms.example.com", value: hrms.hrms_base_url || "" });
  const key = el("input", { id: "hrms-key", type: "password", placeholder: hrms.configured ? "•••• " + (hrms.api_key_hint || "") + " (enter to replace)" : "Company API key with hr:write" });
  return [el("h1", {}, "Settings"),
    el("div", { class: "card" }, el("h2", {}, "Members"),
      el("table", {}, el("tbody", {}, members.items.map((m) => el("tr", { "data-member": m.id }, el("td", {}, m.email), el("td", {}, el("span", { class: "badge" }, m.status)),
        el("td", {}, el("select", { "aria-label": "Role of " + m.email, onchange: async (ev) => { try { await api("PATCH", base() + "/members/" + m.id, { role: ev.target.value }); toast("Role updated"); render(); } catch (e) { toast(e.message, true); render(); } } },
          ["admin", "recruiter", "interviewer", "viewer"].map((r) => el("option", { value: r, selected: r === m.role }, r)))),
        el("td", {}, el("button", { class: "btn small danger", onclick: async () => { try { await api("DELETE", base() + "/members/" + m.id); toast("Removed"); render(); } catch (e) { toast(e.message, true); } } }, "Remove")))))),
      el("form", { id: "invite-form", onsubmit: async (e) => { e.preventDefault(); try { await api("POST", base() + "/members", { email: email.value, role: role.value }); toast("Invitation recorded"); render(); } catch (err) { toast(err.message, true); } } },
        el("div", { class: "row" }, field("Invite by email", email), field("Role", role)), el("button", { class: "btn primary", type: "submit", style: "margin-top:8px" }, "Invite"))),
    el("div", { class: "card" }, el("h2", {}, "HRMS connection"),
      el("p", { class: "muted small" }, "Hires are handed to your HRMS, which creates the employee and runs onboarding. The key is stored encrypted and is never shown again."),
      hrms.configured ? el("p", { id: "hrms-state" }, el("span", { class: "badge success" }, "Connected"), " " + hrms.hrms_base_url) : el("p", { class: "muted" }, "Not connected."),
      el("form", { id: "hrms-form", onsubmit: async (e) => { e.preventDefault(); try { await api("PUT", base() + "/integrations/hrms", { hrms_base_url: url.value, api_key: key.value }); toast("HRMS connection saved"); render(); } catch (err) { toast(err.message, true); } } },
        el("div", { class: "row" }, field("HRMS URL", url), field("API key", key)), el("button", { class: "btn primary", type: "submit", style: "margin-top:8px" }, "Save connection"))),
    el("div", { class: "card" }, el("h2", {}, "Audit log"), el("table", {}, el("tbody", {}, audit.items.slice(0, 50).map((x) => el("tr", {}, el("td", { class: "small muted" }, x.created_at.slice(0, 16).replace("T", " ")), el("td", {}, x.action), el("td", { class: "small muted" }, x.entity_type || ""))))))];
}

/* ---------- render ---------- */
let rendering = 0;
async function render() {
  const token = ++rendering;
  if (!S.cfg) return;
  if (!S.session || !S.me) return renderLogin();
  if (!S.org) return renderWorkspacePicker();
  let content;
  try {
    content = S.view === "jobs" ? viewJobs() : S.view === "candidates" ? await viewCandidates() : S.view === "settings" ? await viewSettings() : viewPipeline();
  } catch (e) { toast(e.message, true); content = el("p", { class: "error" }, e.message); }
  if (token !== rendering) return;
  shell(content);
}
async function boot() {
  try { S.me = await api("GET", "/v1/me"); } catch { S.me = null; return render(); }
  let last = null; try { last = localStorage.getItem("kukhire.org"); } catch { /* optional */ }
  const pick = S.me.organizations.find((o) => o.id === last) || (S.me.organizations.length === 1 ? S.me.organizations[0] : null);
  if (pick) await openOrg(pick.id); else render();
}
(async function start() {
  S.cfg = await fetch("/config.json").then((r) => r.json());
  S.session = store.get();
  if (S.session) await boot(); else render();
})();
