#!/usr/bin/env python3
"""
review_page.py
--------------
The single HTML page served by 21-wrangling/review_app.py. Like report.py it is
dependency-free -- no CDN, no build step -- but unlike report.py it embeds no
data: the page fetches everything from the local server at load time, so the
template itself is safe to keep in the repository.

The page talks to three endpoints:

    GET  /api/state      review periods, items (one per pump cycle, plus other
                         forms), current review state
    POST /api/review     {item_id, decision, reason, reviewer}; decision is
                         approved | rejected | pending | payment_processed |
                         payment_not_processed
    GET  /media/<file>   a localised field photo
"""

HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Submission Review</title>
<style>
:root {
  --bg: #f6f7f9; --panel: #ffffff; --text: #1d2330; --muted: #5d6677;
  --line: #dde1e8; --accent: #2f5fd0; --chip: #eef1f6;
  --ok: #1d7a46; --ok-bg: #e3f4ea; --bad: #b3261e; --bad-bg: #fbe7e5;
  --pend: #7a5b00; --pend-bg: #fdf3d6; --warn: #9a5b00; --late: #a24a00; --late-bg: #fdeadb;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #12151b; --panel: #1b1f27; --text: #e6e9ef; --muted: #9aa3b2;
    --line: #2e3440; --accent: #7aa2ff; --chip: #262c37;
    --ok: #6fd39a; --ok-bg: #173325; --bad: #ff8a80; --bad-bg: #3a1c1a;
    --pend: #f2cf66; --pend-bg: #352c12; --warn: #f2b45c; --late: #ffb27a; --late-bg: #3a2616;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
header { background: var(--panel); border-bottom: 1px solid var(--line);
  padding: 14px 20px; display: flex; flex-wrap: wrap; gap: 12px 24px; align-items: center; }
header h1 { font-size: 17px; margin: 0; }
header .meta { color: var(--muted); font-size: 12px; }
header .spacer { flex: 1; }
label.reviewer { display: flex; gap: 6px; align-items: center; font-size: 13px; }
input, textarea, button, select { font: inherit; color: inherit; }
input[type=text], textarea, select { background: var(--bg); border: 1px solid var(--line);
  border-radius: 6px; padding: 6px 8px; }
a { color: var(--accent); }
main { max-width: 1100px; margin: 0 auto; padding: 16px 20px 60px; }
.toolbar { display: flex; flex-wrap: wrap; gap: 10px 18px; align-items: center; margin-bottom: 12px; }
.toolbar .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em;
  min-width: 70px; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip { border: 1px solid var(--line); background: var(--chip); border-radius: 999px;
  padding: 3px 10px; cursor: pointer; font-size: 13px; }
.chip[aria-pressed=true] { background: var(--accent); border-color: var(--accent); color: #fff; }
.chip .n { opacity: .7; margin-left: 4px; }
details.more { font-size: 13px; }
details.more summary { cursor: pointer; color: var(--accent); }
details.more .chips { margin-top: 8px; }
h2.village { font-size: 16px; margin: 26px 0 4px; }
h3.pump { font-size: 14px; margin: 16px 0 8px; display: flex; flex-wrap: wrap; gap: 4px 12px; align-items: baseline; }
h3.pump small { color: var(--muted); font-weight: normal; font-size: 12px; }
h4.other { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; margin: 12px 0 6px; }
.empty { background: var(--panel); border: 1px dashed var(--line); border-radius: 8px;
  padding: 12px 16px; color: var(--muted); }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
  margin-bottom: 10px; overflow: hidden; }
.card.approved { border-left: 4px solid var(--ok); }
.card.rejected { border-left: 4px solid var(--bad); }
.card.pending { border-left: 4px solid var(--pend); }
.card-head { display: flex; flex-wrap: wrap; gap: 6px 14px; align-items: center; padding: 10px 16px; }
.card-body { border-top: 1px solid var(--line); }
.card-head .title { font-weight: 600; }
.card-head .sub { color: var(--muted); font-size: 13px; }
.badge { border-radius: 4px; padding: 1px 8px; font-size: 12px; font-weight: 600; text-transform: capitalize; }
.badge.approved, .badge.on_time { color: var(--ok); background: var(--ok-bg); }
.badge.rejected, .badge.missed { color: var(--bad); background: var(--bad-bg); }
.badge.pending { color: var(--pend); background: var(--pend-bg); }
.badge.overdue { color: var(--late); background: var(--late-bg); }
.badge.in_progress, .badge.form { color: var(--muted); background: var(--chip); }
.badge.warn { color: var(--warn); background: var(--pend-bg); text-transform: none; font-weight: normal; }
.card-body { display: grid; grid-template-columns: minmax(0, 1fr) 320px; }
@media (max-width: 820px) { .card-body { grid-template-columns: 1fr; } }
.answers { padding: 12px 16px; border-right: 1px solid var(--line); }
.answers table { border-collapse: collapse; width: 100%; }
.answers td { vertical-align: top; padding: 5px 0; border-bottom: 1px solid var(--line); }
.answers tr:last-child td { border-bottom: 0; }
.answers td.q { color: var(--muted); width: 55%; padding-right: 14px; font-size: 13px; }
.answers p { margin: 0; }
.photos { display: flex; flex-wrap: wrap; gap: 6px; }
.photos img { width: 120px; height: 120px; object-fit: cover; border-radius: 6px;
  border: 1px solid var(--line); background: var(--chip); }
.missing { color: var(--muted); font-size: 12px; font-style: italic; }
.decide { padding: 12px 16px; display: flex; flex-direction: column; gap: 8px; }
.decide textarea { width: 100%; min-height: 64px; resize: vertical; }
.decide .buttons { display: flex; gap: 8px; }
.decide button { border: 1px solid var(--line); background: var(--chip); border-radius: 6px;
  padding: 6px 12px; cursor: pointer; }
.decide button.approve { background: var(--ok); border-color: var(--ok); color: #fff; }
.decide button.reject { background: var(--bad); border-color: var(--bad); color: #fff; }
.decide button.reset { margin-left: auto; }
.decide button:disabled { opacity: .5; cursor: default; }
.decide button.pay { align-self: flex-start; }
.decide button.link { border: 0; background: none; color: var(--accent); padding: 0 4px; text-decoration: underline; }
.paid { font-size: 13px; }
.subhead { font-size: 13px; color: var(--muted); margin: 4px 0 6px; }
.subhead + table { margin-bottom: 14px; }
.current { font-size: 13px; }
.current .reason { white-space: pre-wrap; }
.err { color: var(--bad); font-size: 13px; min-height: 1em; }
.history { font-size: 12px; color: var(--muted); }
.history ol { margin: 4px 0 0; padding-left: 18px; }
footer { color: var(--muted); font-size: 12px; margin-top: 28px; }
.notice { color: var(--warn); background: var(--pend-bg); border-radius: 6px; padding: 6px 10px;
  font-size: 13px; margin-bottom: 12px; }
.notice:empty { display: none; }
code { font-size: 12px; }
</style>
</head>
<body>
<header>
  <div>
    <h1>Submission review</h1>
    <div class="meta" id="meta">Loading…</div>
  </div>
  <div class="spacer"></div>
  <label class="reviewer">Reviewer <input type="text" id="reviewer" placeholder="your name" size="16"></label>
  <a href="/export/review_status.csv" download>Download status CSV</a>
</header>
<main>
  <div class="notice" id="orphans"></div>
  <div class="toolbar">
    <span class="label">Period</span>
    <select id="period"></select>
  </div>
  <div class="toolbar">
    <span class="label">Villages</span>
    <div class="chips" id="selected-villages"></div>
    <details class="more"><summary>add village</summary><div class="chips" id="other-villages"></div></details>
  </div>
  <div class="toolbar">
    <span class="label">Timing</span>
    <div class="chips" id="timing-filter"></div>
  </div>
  <div class="toolbar">
    <span class="label">Review</span>
    <div class="chips" id="status-filter"></div>
  </div>
  <div id="list"></div>
  <footer id="footer"></footer>
</main>
<script>
"use strict";
const TZ = "Asia/Kolkata";
const TIMING = {on_time: "On time", overdue: "Overdue", missed: "Missed", in_progress: "In progress"};
let STATE = null;
let period = null;
let selected = [];
let statusFilter = "all";
let timingFilter = "all";

const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const fmt = iso => iso ? new Date(iso).toLocaleString("en-IN",
  {timeZone: TZ, dateStyle: "medium", timeStyle: "short"}) + " IST" : "";
// Dates are India calendar dates already; format without shifting them.
const day = d => d ? new Date(d + "T00:00:00Z").toLocaleDateString("en-IN",
  {timeZone: "UTC", day: "numeric", month: "short", year: "numeric"}) : "";
const dayShort = d => d ? new Date(d + "T00:00:00Z").toLocaleDateString("en-IN",
  {timeZone: "UTC", day: "numeric", month: "short"}) : "";
const isCycle = it => it.kind === "cycle";

function store(key, value) {
  try { if (value === undefined) return localStorage.getItem(key); localStorage.setItem(key, value); }
  catch (e) { return null; }
}

function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  if (p.has("v")) selected = p.getAll("v");
  if (p.has("period")) period = p.get("period");
  if (p.has("status")) statusFilter = p.get("status");
  if (p.has("timing")) timingFilter = p.get("timing");
}
function writeHash() {
  const p = new URLSearchParams();
  p.set("period", period);
  selected.forEach(v => p.append("v", v));
  if (statusFilter !== "all") p.set("status", statusFilter);
  if (timingFilter !== "all") p.set("timing", timingFilter);
  history.replaceState(null, "", "#" + p.toString());
}

async function load() {
  const r = await fetch("/api/state");
  STATE = await r.json();
  if (!r.ok) { document.getElementById("meta").textContent = STATE.error || r.statusText; return; }
  selected = STATE.default_villages.slice();
  period = STATE.default_period;
  readHash();
  if (!STATE.periods.some(p => p.review_date === period)) period = STATE.default_period;
  const m = STATE.meta;
  document.getElementById("meta").innerHTML =
    `Data pulled ${esc(fmt(m.pulled_at))} · rolling ${STATE.cycle_days}-day cycles, as scheduled in Avni · ` +
    `reviews <code>${esc(m.review_dir)}</code>`;
  document.getElementById("footer").innerHTML =
    `One review per pump per cycle. The first cycle is due on the pump's first survey due date; each later ` +
    `cycle is due ${STATE.cycle_days} days after the previous cycle's survey came in, or ${STATE.cycle_days} days ` +
    `after the previous due date if that cycle was missed. A cycle is on time if its survey came in on or before ` +
    `the due date, overdue if up to ${STATE.cycle_days - 1} days later, missed if nothing came in by then, and in ` +
    `progress if that window was still open on the pull date (${esc(day(STATE.as_of_date))}). Further surveys on ` +
    `the same day count towards the same cycle; a survey on a later day fills the next cycle. ` +
    `Every action is appended to <code>${esc(m.log_path)}</code>; ` +
    `<code>${esc(m.status_path)}</code> is the review sheet, one row per pump cycle (open it in Excel or import it into Google Sheets).`;
  const sel = document.getElementById("period");
  sel.innerHTML = STATE.periods.map(p => `<option value="${esc(p.review_date)}">` +
    `Review ${esc(day(p.review_date))} — cycles due ${esc(dayShort(p.start))} to ${esc(dayShort(p.end))}` +
    `${p.complete ? "" : " (not yet due)"}</option>`).join("");
  sel.value = period;
  const n = STATE.orphaned_reviews || 0;
  document.getElementById("orphans").textContent = n ? `${n} reviewed item${n == 1 ? "" : "s"} in the ` +
    `review log no longer match${n == 1 ? "es" : ""} any cycle or form in this data (a re-pull moved the ` +
    `cycle's due date). Their decisions are kept in the log but not shown here.` : "";
  sel.onchange = () => { period = sel.value; writeHash(); render(); };
  render();
}

function reviewOf(id) { return STATE.reviews[id] || {status: "pending", history: [], payment_processed: false}; }
function statusOf(it) { return reviewOf(it.item_id).status; }

function renderChips(id, keys, current, labels, counts, onPick) {
  document.getElementById(id).innerHTML = keys.map(k =>
    `<button class="chip" data-k="${esc(k)}" aria-pressed="${current === k}">${esc(labels[k] || k)}` +
    `<span class="n">${counts[k] || 0}</span></button>`).join("");
  document.querySelectorAll(`#${id} button.chip`).forEach(b => b.onclick = () => onPick(b.dataset.k));
}

function renderVillages(inPeriod) {
  const counts = {};
  inPeriod.forEach(it => counts[it.village] = (counts[it.village] || 0) + 1);
  const chip = v => `<button class="chip" data-v="${esc(v)}" aria-pressed="${selected.includes(v)}">` +
    `${esc(v)}<span class="n">${counts[v] || 0}</span></button>`;
  document.getElementById("selected-villages").innerHTML =
    selected.length ? selected.map(chip).join("") : `<span class="missing">none selected</span>`;
  document.getElementById("other-villages").innerHTML =
    STATE.villages.filter(v => !selected.includes(v)).map(chip).join("");
  document.querySelectorAll("button.chip[data-v]").forEach(b => b.onclick = () => {
    const v = b.dataset.v;
    selected = selected.includes(v) ? selected.filter(x => x !== v) : selected.concat([v]);
    writeHash(); render();
  });
}

function answersHtml(sub) {
  if (!sub.answers.length) return `<p class="missing">No answers recorded.</p>`;
  return "<table>" + sub.answers.map(a => {
    let val = a.values.map(esc).join(", ");
    if (a.photos.length) val += `<div class="photos">` + a.photos.map(f =>
      `<a href="/media/${encodeURIComponent(f)}" target="_blank" rel="noopener">` +
      `<img loading="lazy" src="/media/${encodeURIComponent(f)}" alt=""></a>`).join("") + `</div>`;
    if (a.photos_missing) val += `<div class="missing">${a.photos_missing} photo(s) not available ` +
      `(not downloaded, or missing from the bucket)</div>`;
    return `<tr><td class="q">${esc(a.question)}</td><td>${val}</td></tr>`;
  }).join("") + "</table>";
}

function timingBadge(it) {
  if (it.kind === "other_form") return `<span class="badge form">${esc(it.submissions[0].encounter_type)}</span>`;
  let label = TIMING[it.timeliness];
  if (it.timeliness === "overdue") label += ` · ${it.days_late} day${it.days_late == 1 ? "" : "s"} late`;
  return `<span class="badge ${esc(it.timeliness)}">${esc(label)}</span>`;
}

function decideHtml(it) {
  const rv = reviewOf(it.item_id);
  const subs = it.submissions;
  const modified = rv.reviewed_at && subs.some(s => s.last_modified_at && s.last_modified_at > rv.reviewed_at);
  const decided = rv.status === "approved" || rv.status === "rejected";
  const current = !decided ? `<div class="current missing">Not reviewed yet.</div>` :
    `<div class="current">Payment <span class="badge ${rv.status}">${rv.status}</span> by ${esc(rv.reviewer)}, ` +
    `${esc(fmt(rv.reviewed_at))}${rv.reason ? `<div class="reason">“${esc(rv.reason)}”</div>` : ""}</div>`;
  const hist = rv.history.length > 1 ? `<details class="history"><summary>${rv.history.length} actions</summary><ol>` +
    rv.history.map(h => `<li>${esc(h.decision.replace(/_/g, " "))} · ${esc(h.reviewer)} · ${esc(fmt(h.recorded_at))}` +
      `${h.reason ? " · “" + esc(h.reason) + "”" : ""}</li>`).join("") + `</ol></details>` : "";
  const payment = rv.status !== "approved" ? "" : rv.payment_processed
    ? `<div class="paid"><span class="badge approved">Payment processed</span> ${esc(rv.payment_by || "")}, ${esc(fmt(rv.payment_at))}
       <button class="link" data-d="payment_not_processed">undo</button></div>`
    : `<button class="pay" data-d="payment_processed">Mark payment processed</button>`;
  return `<div class="decide">
      ${current}${hist}
      ${modified ? `<span class="badge warn">edited in Avni after review</span>` : ""}
      <textarea placeholder="Remarks / issues (required to reject)">${decided ? esc(rv.reason) : ""}</textarea>
      <div class="buttons">
        <button class="approve" data-d="approved">Approve payment</button>
        <button class="reject" data-d="rejected">Reject</button>
        ${rv.status !== "pending" ? `<button class="reset" data-d="pending" title="Withdraw the decision">Reset</button>` : ""}
      </div>
      ${payment}
      <div class="err"></div>
    </div>`;
}

function cardHtml(it) {
  const st = statusOf(it);
  const subs = it.submissions;
  const cycle = isCycle(it) ? `<span class="title">Cycle ${it.cycle_number}</span>` +
    `<span class="sub">due ${esc(day(it.cycle_due_date))} · next ${esc(day(it.next_due_date))}</span>` : "";
  const head = `<div class="card-head">
      ${cycle}${timingBadge(it)}
      ${isCycle(it) ? `<span class="sub">${subs.length} submission${subs.length == 1 ? "" : "s"}</span>` : ""}
      <span class="badge ${st}">${st}</span>
    </div>`;
  let body;
  if (subs.length) {
    body = subs.map(s => `<div class="subhead">Submitted ${esc(fmt(s.completed_at))} by <b>${esc(s.submitted_by)}</b>` +
      ` · <code>${esc(s.encounter_id)}</code></div>${answersHtml(s)}`).join("");
  } else if (it.timeliness === "in_progress") {
    body = `<p>No survey yet. The cycle runs to ${esc(day(it.cycle_end_date))}.</p>`;
  } else {
    body = `<p>No survey came in for this cycle by ${esc(day(it.cycle_end_date))}.</p>`;
  }
  return `<article class="card ${st}" data-id="${esc(it.item_id)}">${head}
    <div class="card-body"><div class="answers">${body}</div>${decideHtml(it)}</div></article>`;
}

function pumpSummary(items) {
  const c = {};
  items.filter(isCycle).forEach(i => c[i.timeliness] = (c[i.timeliness] || 0) + 1);
  return Object.keys(TIMING).filter(k => c[k]).map(k => `${c[k]} ${TIMING[k].toLowerCase()}`).join(" · ");
}

function render() {
  const inPeriod = STATE.items.filter(it => it.review_period === period);
  renderVillages(inPeriod);
  const scoped = inPeriod.filter(it => selected.includes(it.village));

  const tCounts = {all: scoped.length};
  scoped.forEach(it => { const k = isCycle(it) ? it.timeliness : "other"; tCounts[k] = (tCounts[k] || 0) + 1; });
  renderChips("timing-filter", ["all", "on_time", "overdue", "missed", "in_progress", "other"], timingFilter,
    {all: "All", other: "Other forms", ...TIMING}, tCounts, k => { timingFilter = k; writeHash(); render(); });
  const timed = scoped.filter(it => timingFilter === "all" ||
    (timingFilter === "other" ? !isCycle(it) : it.timeliness === timingFilter));

  const sCounts = {all: timed.length};
  timed.forEach(it => sCounts[statusOf(it)] = (sCounts[statusOf(it)] || 0) + 1);
  renderChips("status-filter", ["all", "pending", "approved", "rejected"], statusFilter,
    {all: "All", pending: "Pending", approved: "Approved", rejected: "Rejected"}, sCounts,
    k => { statusFilter = k; writeHash(); render(); });
  const shown = timed.filter(it => statusFilter === "all" || statusOf(it) === statusFilter);

  const html = selected.map(v => {
    const head = `<h2 class="village">${esc(v)}</h2>`;
    const unscheduled = STATE.unscheduled_pumps.filter(p => p.village === v)
      .map(p => `<div class="empty">${esc(p.subject_name || "(unnamed)")}: no survey schedule in Avni, so no cycles.</div>`).join("");
    const vItems = shown.filter(it => it.village === v);
    if (!vItems.length) {
      const why = inPeriod.some(it => it.village === v) ? "Nothing matches the filters." : "No cycles or submissions in this period.";
      return head + unscheduled + `<div class="empty">${why}</div>`;
    }
    const pumps = [...new Set(vItems.map(it => it.subject_id))];
    return head + unscheduled + pumps.map(pid => {
      const pItems = vItems.filter(it => it.subject_id === pid);
      const all = inPeriod.filter(it => it.subject_id === pid);
      const others = pItems.filter(it => !isCycle(it));
      return `<h3 class="pump">${esc(pItems[0].subject_name || "(unnamed pump)")}<small>${esc(pumpSummary(all))}</small></h3>` +
        pItems.filter(isCycle).map(cardHtml).join("") +
        (others.length ? `<h4 class="other">Other forms</h4>` + others.map(cardHtml).join("") : "");
    }).join("");
  }).join("");
  document.getElementById("list").innerHTML = html || `<div class="empty">Pick a village above.</div>`;
  document.querySelectorAll(".card button[data-d]").forEach(b => b.onclick = () => decide(b));
}

async function decide(button) {
  const card = button.closest(".card");
  const err = card.querySelector(".err");
  const reviewer = document.getElementById("reviewer").value.trim();
  const decision = button.dataset.d;
  const isPayment = decision.startsWith("payment_");
  const reason = isPayment ? "" : card.querySelector("textarea").value.trim();
  err.textContent = "";
  if (!reviewer) { err.textContent = "Enter your name as reviewer (top right) first."; return; }
  if (decision === "rejected" && !reason) { err.textContent = "A rejection needs a reason."; return; }
  card.querySelectorAll("button").forEach(b => b.disabled = true);
  try {
    const r = await fetch("/api/review", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({item_id: card.dataset.id, decision, reason, reviewer})});
    const body = await r.json();
    if (!r.ok) throw new Error(body.error || r.statusText);
    STATE.reviews[card.dataset.id] = body.review;
    if (body.warning) alert(body.warning);
    render();
  } catch (e) {
    err.textContent = "Not saved: " + e.message;
    card.querySelectorAll("button").forEach(b => b.disabled = false);
  }
}

const rev = document.getElementById("reviewer");
rev.value = store("review.reviewer") || "";
rev.addEventListener("change", () => store("review.reviewer", rev.value.trim()));
load().then(() => { if (!rev.value && STATE) rev.value = STATE.default_reviewer || ""; });
</script>
</body>
</html>
"""
