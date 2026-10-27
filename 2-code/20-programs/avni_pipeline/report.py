#!/usr/bin/env python3
"""
report.py
---------
Turns the CSV table set into ONE self-contained interactive HTML file: no CDN,
no external CSS/JS, no build step -- open it in a browser or email it.

It reads the tables (in memory from transform.py, or re-read from disk by
emit_csv.read_tables), builds a compact JSON model and embeds it next to a small
dependency-free UI: KPIs, filters, hand-rolled SVG charts with tooltips, an
operator-consistency table, a searchable/expandable encounter list, a data-quality
panel and a CSV export of whatever is currently filtered.

The only remote things on the page are the observation photo URLs, which are
private S3 objects (they 403 without auth) -- each thumbnail falls back to a
plain link if it fails to load, so the page is fully usable offline.

USAGE
-----
    from avni_pipeline import report

    CLEAN = "1-data/12-avni/121-clean"
    report.write_report(tables, CLEAN + "/report.html")          # from a TableSet
    report.write_report(emit_csv.read_tables(CLEAN), CLEAN + "/report.html")
"""

import datetime
import json
import os

from . import transform

SURVEY = transform.SURVEY
GRIEVANCE = transform.GRIEVANCE
PAYMENT = transform.PAYMENT

# Derived (computed, not asked) numeric columns worth charting over time.
DERIVED_METRICS = [
    ("pump_runtime_hours", "hours"),
    ("days_since_last_refill", "days"),
    ("submission_lag_days", "days"),
    ("pump_days_since_prev_submission", "days"),
]


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #

def _iso(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime.datetime):
        return value.astimezone(datetime.timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z")
    if isinstance(value, datetime.date):
        return value.isoformat()
    return str(value)


def _observations_by_encounter(tables):
    """Rebuild per-encounter observation lists from observations_long."""
    grouped = {}
    if "observations_long" not in tables:
        return grouped
    for row in tables["observations_long"].rows:
        per_encounter = grouped.setdefault(row["encounter_id"], {})
        entry = per_encounter.get(row["concept_name"])
        if entry is None:
            entry = {"key": row["concept_name"], "col": row["concept_column"] or "",
                     "values": [], "media": [], "num": None, "isList": bool(row["is_list"])}
            per_encounter[row["concept_name"]] = entry
        entry["values"].append(row["value_text"])
        if row.get("media_url"):
            entry["media"].append(row["media_url"])
        if row.get("value_num") is not None and entry["num"] is None and not entry["isList"]:
            entry["num"] = row["value_num"]
    out = {}
    for encounter_id, concepts_map in grouped.items():
        items = []
        for entry in concepts_map.values():
            values = entry["values"]
            items.append({
                "key": entry["key"], "col": entry["col"],
                "value": "|".join(values) if entry["isList"] else (values[0] if values else ""),
                "list": values if entry["isList"] else None,
                "media": entry["media"], "num": entry["num"],
            })
        items.sort(key=lambda item: item["key"])
        out[encounter_id] = items
    return out


def _survey_extras(tables):
    """Derived numeric metrics + QC flags per survey encounter."""
    extras = {}
    if SURVEY not in tables:
        return extras
    for row in tables[SURVEY].rows:
        derived, flags = [], []
        for column, unit in DERIVED_METRICS:
            value = row.get(column)
            if value is not None:
                derived.append({"key": f"{column} ({unit})", "col": column,
                                "value": _iso(value), "list": None, "media": [],
                                "num": float(value), "derived": True})
        for column in ("chlorine_out_of_range_flag", "pump_runtime_implausible_flag",
                       "refill_in_future_flag"):
            if row.get(column) is True:
                flags.append(column.replace("_flag", ""))
        extras[row["encounter_id"]] = {"derived": derived, "flags": flags}
    return extras


def build_model(tables):
    """Compact JSON model embedded in the report (all values already stringified)."""
    obs = _observations_by_encounter(tables)
    extras = _survey_extras(tables)

    subjects = [{
        "id": r["subject_id"], "name": r["subject_name"], "type": r["subject_type"],
        "village": r.get("village"), "district": r.get("district"), "state": r.get("state"),
        "registered": _iso(r.get("registration_date")),
        "nEnc": r.get("n_encounters"), "nCompleted": r.get("n_completed"),
        "operators": r.get("distinct_operators"),
        "externalId": r.get("subject_external_id"),
    } for r in tables["subjects"].rows] if "subjects" in tables else []

    encounters = []
    for r in tables["encounters"].rows if "encounters" in tables else []:
        extra = extras.get(r["encounter_id"], {})
        encounters.append({
            "id": r["encounter_id"], "sid": r["subject_id"], "sname": r["subject_name"],
            "stype": r["subject_type"], "type": r["encounter_type"],
            "ver": r.get("form_version") or "", "status": r["status"],
            "sched": _iso(r.get("scheduled_date")), "comp": _iso(r.get("completed_at")),
            "cancel": _iso(r.get("cancelled_at")), "by": r.get("submitted_by") or "",
            "mod": r.get("last_modified_by") or "",
            "created": r.get("created_by") or "",
            "lag": r.get("submission_lag_days"),
            "onTime": r.get("is_on_time"),
            "obs": obs.get(r["encounter_id"], []) + extra.get("derived", []),
            "flags": extra.get("flags", []),
        })

    operators = [{
        "operator": r["operator"], "n": r["n_submissions"], "surveys": r["n_surveys"],
        "grievances": r["n_grievances"], "subjects": r["distinct_subjects"],
        "activeDays": r["active_days"], "meanGap": r["mean_gap_days"],
        "gapCv": r["gap_cv"], "onTime": r["on_time_rate"],
        "first": _iso(r["first_submission_at"]), "last": _iso(r["last_submission_at"]),
    } for r in tables["operators"].rows] if "operators" in tables else []

    table_index = [{"name": name, "rows": len(t.rows), "cols": len(t.columns),
                    "grain": t.grain} for name, t in tables.tables.items()]

    meta = {k: _iso(v) if isinstance(v, (datetime.date, datetime.datetime)) else v
            for k, v in (tables.meta or {}).items()}
    return {"meta": meta, "quality": tables.quality or {}, "subjects": subjects,
            "encounters": encounters, "operators": operators, "tables": table_index}


# --------------------------------------------------------------------------- #
# Render
# --------------------------------------------------------------------------- #

def build_html(tables, title=None):
    """Return the complete HTML document as a string."""
    model = build_model(tables)
    data_json = json.dumps(model, ensure_ascii=False, default=str).replace("</", "<\\/")
    source = model["meta"].get("source", "avni")
    generated = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    heading = title or "Avni pump data"
    return (HTML_TEMPLATE
            .replace("__DATA_JSON__", data_json)
            .replace("__TITLE__", _escape(heading))
            .replace("__GENERATED__", generated)
            .replace("__SOURCE__", _escape(str(source))))


def write_report(tables, path, title=None):
    """Write the self-contained report to ``path`` and return the path."""
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(build_html(tables, title))
    return path


def _escape(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# --------------------------------------------------------------------------- #
# The page (single file: markup + CSS + JS, no external assets)
# --------------------------------------------------------------------------- #

HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ - Avni report</title>
<style>
  :root{
    --page:#f7f7f5; --card:#ffffff; --surface:#fbfbf9;
    --ink:#111110; --ink-2:#55544f; --muted:#8b8981;
    --line:rgba(17,17,16,.12); --grid:#e4e3dc; --axis:#c6c5ba;
    --s1:#2a78d6; --s2:#1baf7a; --s3:#d99000; --s4:#7a4fd0;
    --s5:#e05f4a; --s6:#0f9aa8; --s7:#c2568f; --s8:#6b7f00;
    --ok:#0a9c4a; --warn:#e0a020; --bad:#d0453b;
  }
  @media (prefers-color-scheme: dark){
    :root:not([data-theme="light"]){
      --page:#0e0e0d; --card:#1a1a19; --surface:#211f1e;
      --ink:#f5f4f1; --ink-2:#c2c0b7; --muted:#8b8981;
      --line:rgba(245,244,241,.13); --grid:#2b2b29; --axis:#3a3a36;
      --s1:#5b9df0; --s2:#3cc294; --s3:#e0aa3c; --s4:#a68af0;
      --s5:#f07f66; --s6:#3ec0cd; --s7:#e07cae; --s8:#9db733;
    }
  }
  :root[data-theme="dark"]{
    --page:#0e0e0d; --card:#1a1a19; --surface:#211f1e;
    --ink:#f5f4f1; --ink-2:#c2c0b7; --muted:#8b8981;
    --line:rgba(245,244,241,.13); --grid:#2b2b29; --axis:#3a3a36;
    --s1:#5b9df0; --s2:#3cc294; --s3:#e0aa3c; --s4:#a68af0;
    --s5:#f07f66; --s6:#3ec0cd; --s7:#e07cae; --s8:#9db733;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--page);color:var(--ink);font-size:14px;line-height:1.45;
    font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
  a{color:var(--s1)}
  .wrap{max-width:1200px;margin:0 auto;padding:22px 18px 72px}
  h1{font-size:21px;margin:0 0 3px;letter-spacing:-.01em}
  h2{font-size:14px;margin:0 0 12px;font-weight:650}
  .sub{color:var(--ink-2);font-size:13px}
  .mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;color:var(--muted)}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:15px}
  header.top{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:14px}
  .chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
  .chip{font-size:12px;background:var(--surface);border:1px solid var(--line);
    border-radius:999px;padding:3px 10px;color:var(--ink-2)}
  .chip b{color:var(--ink);font-weight:650}
  .btn{cursor:pointer;background:var(--surface);border:1px solid var(--line);border-radius:8px;
    padding:7px 12px;color:var(--ink);font:inherit;font-size:13px}
  .btn:hover{border-color:var(--muted)}
  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:11px;margin:14px 0}
  .kpi .n{font-size:25px;font-weight:650;letter-spacing:-.02em}
  .kpi .n.sm{font-size:15px}
  .kpi .l{font-size:12px;color:var(--ink-2);margin-top:2px}
  .kpi .d{font-size:11px;color:var(--muted);margin-top:3px}
  .filters{display:flex;flex-wrap:wrap;gap:13px;align-items:flex-end}
  .field{display:flex;flex-direction:column;gap:4px}
  .field label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
  input,select{font:inherit;font-size:13px;color:var(--ink);background:var(--surface);
    border:1px solid var(--line);border-radius:8px;padding:7px 9px;max-width:260px}
  input[type=search]{min-width:210px}
  .typechips{display:flex;flex-wrap:wrap;gap:6px}
  .tc{cursor:pointer;font-size:12px;border:1px solid var(--line);border-radius:999px;
    padding:4px 11px 4px 8px;display:inline-flex;align-items:center;gap:6px;user-select:none;
    background:var(--surface);color:var(--ink-2)}
  .tc .dot{width:9px;height:9px;border-radius:50%}
  .tc.off{opacity:.42}
  .charts{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin:14px 0}
  .chartcard h2{display:flex;justify-content:space-between;align-items:center;gap:10px}
  .chartcard select{font-size:12px;padding:4px 7px;max-width:58%}
  .wide{grid-column:1 / -1}
  .chart{width:100%;overflow:visible;display:block}
  .grid-line{stroke:var(--grid);stroke-width:1}
  .axis-line{stroke:var(--axis);stroke-width:1}
  .tick{fill:var(--muted);font-size:11px}
  .dlabel{fill:var(--ink-2);font-size:11px;font-variant-numeric:tabular-nums}
  .empty{color:var(--muted);font-size:13px;padding:26px 0;text-align:center}
  table{width:100%;border-collapse:collapse}
  thead th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.05em;
    color:var(--muted);font-weight:650;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
  thead th[data-sort]{cursor:pointer}
  thead th[data-sort]:hover{color:var(--ink-2)}
  tbody td{padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}
  tbody tr.rowmain{cursor:pointer}
  tbody tr.rowmain:hover{background:var(--surface)}
  .tdot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:7px}
  td.num{font-variant-numeric:tabular-nums;color:var(--ink-2);white-space:nowrap}
  .badge{font-size:11px;padding:2px 8px;border-radius:999px;font-weight:650;white-space:nowrap;color:#fff}
  .b-completed{background:var(--ok)} .b-planned{background:var(--warn);color:#2b2000}
  .b-cancelled{background:var(--bad)}
  .flag{font-size:11px;padding:1px 7px;border-radius:999px;border:1px solid var(--warn);
    color:var(--warn);margin-left:5px;white-space:nowrap}
  .detail{background:var(--surface)} .detail td{padding:0}
  .obs{padding:13px 16px}
  .obs .o{display:grid;grid-template-columns:minmax(170px,340px) 1fr;gap:6px 18px;padding:5px 0;
    border-bottom:1px dashed var(--line)}
  .obs .o:last-child{border-bottom:0}
  .obs .k{color:var(--ink-2)} .obs .v{word-break:break-word}
  .obs .k.der{color:var(--s4)}
  .pill{display:inline-block;background:var(--card);border:1px solid var(--line);
    border-radius:6px;padding:1px 8px;margin:2px 4px 2px 0;font-size:12px}
  .thumbs{display:flex;flex-wrap:wrap;gap:8px;margin-top:5px}
  .thumbs img{height:68px;width:68px;object-fit:cover;border-radius:8px;border:1px solid var(--line);display:block}
  .qc{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:10px 22px}
  .qc .qrow{display:flex;justify-content:space-between;gap:12px;border-bottom:1px dashed var(--line);padding:5px 0}
  .qc .qrow b{font-variant-numeric:tabular-nums}
  .warn{border-left:3px solid var(--warn);background:var(--surface);padding:9px 12px;
    border-radius:0 8px 8px 0;margin-bottom:8px;font-size:13px}
  .foot{color:var(--muted);font-size:12px;margin-top:26px;text-align:center}
  #tip{position:fixed;pointer-events:none;z-index:50;background:var(--card);border:1px solid var(--line);
    border-radius:8px;padding:7px 10px;font-size:12px;box-shadow:0 8px 26px rgba(0,0,0,.2);
    opacity:0;transition:opacity .08s;max-width:280px}
  #tip .tt{font-weight:650} #tip .ts{color:var(--ink-2)}
  .scroll{overflow-x:auto}
  @media (max-width:860px){ .charts{grid-template-columns:1fr} .hide-sm{display:none} }
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div>
      <h1>__TITLE__</h1>
      <div class="sub" id="headSub"></div>
      <div class="chips" id="headChips"></div>
    </div>
    <button class="btn" id="themeBtn" title="Toggle light / dark">Theme</button>
  </header>

  <div class="kpis" id="kpis"></div>

  <div class="card">
    <div class="filters">
      <div class="field"><label>Search</label>
        <input type="search" id="q" placeholder="pump, user, answer text..."></div>
      <div class="field"><label>Subject</label><select id="subjectSel"></select></div>
      <div class="field"><label>Submitted by</label><select id="userSel"></select></div>
      <div class="field"><label>Status</label><select id="statusSel">
        <option value="">All</option><option value="completed">Completed</option>
        <option value="planned">Planned</option><option value="cancelled">Cancelled</option>
      </select></div>
      <div class="field"><label>From</label><input type="date" id="from"></div>
      <div class="field"><label>To</label><input type="date" id="to"></div>
      <div class="field" style="flex:1;min-width:220px"><label>Encounter type</label>
        <div class="typechips" id="typeChips"></div></div>
      <button class="btn" id="reset">Reset</button>
    </div>
  </div>

  <div class="charts">
    <div class="card chartcard"><h2><span>Metric over time</span>
      <select id="metricSel"></select></h2><div id="chartMetric"></div></div>
    <div class="card chartcard"><h2>Encounters by type</h2><div id="chartTypes"></div></div>
    <div class="card chartcard wide"><h2><span>Submissions by operator</span>
      <span class="sub" id="userHint"></span></h2><div id="chartUsers"></div></div>
    <div class="card chartcard wide"><h2><span>Encounters by pump</span>
      <span class="sub" id="subjHint"></span></h2><div id="chartSubjects"></div></div>
    <div class="card chartcard wide"><h2><span>Answer frequency</span>
      <select id="freqSel"></select></h2><div id="chartFreq"></div></div>
  </div>

  <div class="card" style="margin-bottom:14px">
    <h2>Operator consistency <span class="sub">(from operators.csv; gap CV = steadiness, lower is steadier)</span></h2>
    <div class="scroll"><table>
      <thead><tr><th>Operator</th><th class="num">Submissions</th><th class="num">Surveys</th>
        <th class="num">Pumps</th><th class="num">Active days</th><th class="num">Mean gap (d)</th>
        <th class="num">Gap CV</th><th class="num">On time</th><th class="hide-sm">Window</th></tr></thead>
      <tbody id="opBody"></tbody></table></div>
  </div>

  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:10px">
      <h2 style="margin:0">Encounters <span class="sub" id="count"></span></h2>
      <button class="btn" id="csv">Download filtered CSV</button>
    </div>
    <div class="scroll"><table>
      <thead><tr>
        <th data-sort="sname">Pump</th>
        <th data-sort="type">Type</th>
        <th data-sort="by">Submitted by</th>
        <th data-sort="status">Status</th>
        <th data-sort="sched" class="hide-sm">Scheduled</th>
        <th data-sort="comp">Completed</th>
        <th data-sort="nobs" class="num hide-sm">Obs</th>
      </tr></thead>
      <tbody id="tbody"></tbody></table></div>
  </div>

  <div class="card" style="margin-top:14px">
    <h2>Data quality</h2>
    <div id="qcWarnings"></div>
    <div class="qc" id="qcGrid"></div>
  </div>

  <div class="card" style="margin-top:14px">
    <h2>Tables in this export</h2>
    <div class="scroll"><table>
      <thead><tr><th>Table</th><th class="num">Rows</th><th class="num">Columns</th><th>Grain</th></tr></thead>
      <tbody id="tblBody"></tbody></table></div>
  </div>

  <div class="foot" id="foot"></div>
</div>
<div id="tip"></div>

<script>
const DATA = __DATA_JSON__;
const GENERATED = "__GENERATED__";
const SOURCE = "__SOURCE__";
const SVGNS = "http://www.w3.org/2000/svg";

const ENCS = DATA.encounters || [];
const SUBJECTS = DATA.subjects || [];
const OPERATORS = DATA.operators || [];
const QUALITY = DATA.quality || {};
const META = DATA.meta || {};
const subjById = Object.fromEntries(SUBJECTS.map(s => [s.id, s]));
const USERS = [...new Set(ENCS.map(e => e.by).filter(Boolean))].sort();
const TYPES = [...new Set(ENCS.map(e => e.type))].sort();
const typeColor = {};
TYPES.forEach((t, i) => typeColor[t] = "var(--s" + ((i % 8) + 1) + ")");
const STATUS_ORDER = {completed: 0, planned: 1, cancelled: 2};

const $ = sel => document.querySelector(sel);
function svg(tag, attrs){
  const el = document.createElementNS(SVGNS, tag);
  for(const k in attrs) el.setAttribute(k, attrs[k]);
  return el;
}
function esc(s){
  return String(s == null ? "" : s).replace(/[&<>"']/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
function encDate(e){ return e.comp || e.sched || e.cancel || null; }
/* Calendar days are shown in the run's timezone (manifest.timezone), never the
   viewer's and never the UTC date part: a bare yyyy-mm-dd is already a local date;
   a UTC timestamp is converted to the run timezone before taking its day. */
const DAY_FMT = (() => {
  try {
    return new Intl.DateTimeFormat("en-CA", {timeZone: META.timezone || "Asia/Kolkata",
      year: "numeric", month: "2-digit", day: "2-digit"});
  } catch(err) { return null; }
})();
function dayOf(s){
  if(!s) return null;
  if(s.length <= 10 || !DAY_FMT) return s.slice(0, 10);
  const d = new Date(s);
  return isNaN(d) ? s.slice(0, 10) : DAY_FMT.format(d);
}
function fmtDay(s){
  if(!s) return "-";
  const d = new Date(dayOf(s) + "T00:00:00");
  return isNaN(d) ? s : d.toLocaleDateString(undefined, {month:"short", day:"numeric", year:"numeric"});
}
function fmtShort(s){
  if(!s) return "-";
  const d = new Date(dayOf(s) + "T00:00:00");
  return isNaN(d) ? s : d.toLocaleDateString(undefined, {month:"short", day:"numeric"});
}
function num(v, digits){
  if(v === null || v === undefined || v === "") return "-";
  const n = Number(v);
  return isNaN(n) ? String(v) : (digits === undefined ? String(n) : n.toFixed(digits));
}

/* ---- concept catalogs for the two chart pickers ---- */
function buildCatalogs(){
  const numeric = new Map(), stats = new Map();
  for(const e of ENCS){
    for(const o of e.obs){
      if(o.num !== null && o.num !== undefined) numeric.set(o.key, (numeric.get(o.key) || 0) + 1);
      if(o.derived) continue;
      let st = stats.get(o.key);
      if(!st){ st = {count:0, distinct:new Set(), listy:false, mediaOnly:true}; stats.set(o.key, st); }
      st.count++;
      const media = o.media || [];
      if(o.list){
        const plain = o.list.filter(x => !media.includes(x));
        if(plain.length){ st.listy = true; st.mediaOnly = false; plain.forEach(v => st.distinct.add(String(v).trim())); }
      } else if(!media.length){
        st.mediaOnly = false;
        if(o.num === null || o.num === undefined) st.distinct.add(String(o.value).trim());
      }
    }
  }
  const cat = [];
  for(const [key, st] of stats){
    if(st.mediaOnly) continue;
    const nd = st.distinct.size;
    if(st.listy || (nd > 0 && nd <= 15 && nd / st.count < 0.7)) cat.push([key, st.count]);
  }
  cat.sort((a, b) => b[1] - a[1]);
  return {
    numeric: [...numeric.entries()].sort((a, b) => b[1] - a[1]).map(x => x[0]),
    cat: cat.map(x => x[0]),
  };
}
const CATALOG = buildCatalogs();

/* ---- tooltip ---- */
const tip = $("#tip");
function showTip(html, x, y){
  tip.innerHTML = html; tip.style.opacity = 1;
  tip.style.left = Math.min(x + 14, innerWidth - tip.offsetWidth - 8) + "px";
  tip.style.top = Math.max(8, y - tip.offsetHeight - 12) + "px";
}
function hideTip(){ tip.style.opacity = 0; }

/* ---- state ---- */
const state = {
  q:"", status:"", from:"", to:"", subject:"", user:"",
  types: new Set(TYPES),
  metric: CATALOG.numeric.find(n => /chlorine/i.test(n)) || CATALOG.numeric[0] || null,
  freq: CATALOG.cat.find(n => /grievance/i.test(n)) || CATALOG.cat[0] || null,
  sort: "comp", dir: -1,
};

function filtered(){
  const q = state.q.trim().toLowerCase();
  return ENCS.filter(e => {
    if(state.subject && e.sid !== state.subject) return false;
    if(state.user && (e.by || "") !== state.user) return false;
    if(!state.types.has(e.type)) return false;
    if(state.status && e.status !== state.status) return false;
    const d = dayOf(encDate(e));
    if(state.from && (!d || d < state.from)) return false;
    if(state.to && (!d || d > state.to)) return false;
    if(q){
      let hay = (e.type + " " + (e.sname || "") + " " + (e.by || "")).toLowerCase();
      for(const o of e.obs) hay += " " + o.key.toLowerCase() + " " + String(o.value).toLowerCase();
      if(!hay.includes(q)) return false;
    }
    return true;
  });
}

/* ---- header + KPIs ---- */
function renderHeader(){
  const scope = state.subject ? subjById[state.subject] : null;
  const chips = [];
  if(scope){
    const where = [scope.village, scope.district, scope.state].filter(Boolean).join(" - ");
    $("#headSub").innerHTML = esc(scope.name) + " (" + esc(scope.type) + ")" +
      (where ? " &middot; " + esc(where) : "") + ' <span class="mono">' + esc(scope.id) + "</span>";
    chips.push("<span class='chip'>1 of <b>" + SUBJECTS.length + "</b> subjects</span>");
    if(scope.registered) chips.push("<span class='chip'>Registered <b>" + esc(fmtDay(scope.registered)) + "</b></span>");
    chips.push("<span class='chip'>Encounters <b>" + scope.nEnc + "</b></span>");
    chips.push("<span class='chip'>Operators <b>" + scope.operators + "</b></span>");
  } else {
    const regions = [...new Set(SUBJECTS.map(s => s.district || s.state).filter(Boolean))];
    $("#headSub").innerHTML = "Source <b>" + esc(SOURCE) + "</b> &middot; pulled " +
      esc(META.pulled_at || "-") + " &middot; times local to " + esc(META.timezone || "UTC") +
      (regions.length ? " &middot; " + esc(regions.slice(0, 4).join(", ")) : "");
    chips.push("<span class='chip'>Subjects <b>" + SUBJECTS.length + "</b></span>");
    chips.push("<span class='chip'>Encounters <b>" + ENCS.length + "</b></span>");
    chips.push("<span class='chip'>Types <b>" + TYPES.length + "</b></span>");
    chips.push("<span class='chip'>Operators <b>" + OPERATORS.length + "</b></span>");
  }
  $("#headChips").innerHTML = chips.join("");
}

function renderKpis(rows){
  const by = s => rows.filter(e => e.status === s).length;
  const done = rows.filter(e => e.comp).map(e => e.comp).sort();
  const span = done.length ? fmtShort(done[0]) + " - " + fmtShort(done[done.length - 1]) : "-";
  const chlorine = [];
  for(const e of rows) for(const o of e.obs)
    if(/chlorine reading/i.test(o.key) && o.num !== null && o.num !== undefined) chlorine.push(o.num);
  chlorine.sort((a, b) => a - b);
  const median = chlorine.length
    ? (chlorine.length % 2 ? chlorine[(chlorine.length - 1) / 2]
       : (chlorine[chlorine.length / 2 - 1] + chlorine[chlorine.length / 2]) / 2) : null;
  const items = [
    {n: rows.length, l: "Encounters (filtered)", d: ENCS.length + " total"},
    {n: new Set(rows.map(e => e.sid)).size, l: "Pumps in view", d: SUBJECTS.length + " total"},
    {n: by("completed"), l: "Completed"},
    {n: by("planned"), l: "Planned"},
    {n: new Set(rows.map(e => e.by).filter(Boolean)).size, l: "Operators in view", d: USERS.length + " total"},
    {n: median === null ? "-" : median.toFixed(2), l: "Median chlorine (mg/L)", d: chlorine.length + " readings"},
    {n: span, l: "Completed span", small: true},
  ];
  $("#kpis").innerHTML = items.map(k =>
    '<div class="card kpi"><div class="n' + (k.small ? " sm" : "") + '">' + esc(k.n) + "</div>" +
    '<div class="l">' + esc(k.l) + "</div>" +
    (k.d ? '<div class="d">' + esc(k.d) + "</div>" : "") + "</div>").join("");
}

/* ---- charts ---- */
function renderMetric(rows){
  const host = $("#chartMetric"); host.innerHTML = "";
  const metric = state.metric;
  if(!metric){ host.innerHTML = '<div class="empty">No numeric observations.</div>'; return; }
  const pts = [];
  for(const e of rows){
    const d = encDate(e); if(!d) continue;
    for(const o of e.obs)
      if(o.key === metric && o.num !== null && o.num !== undefined)
        pts.push({t: new Date(d).getTime(), y: o.num, e: e});
  }
  pts.sort((a, b) => a.t - b.t);
  if(!pts.length){
    host.innerHTML = '<div class="empty">No values for this metric in the current filter.</div>'; return;
  }
  const W = host.clientWidth || 560, H = 235, m = {t:14, r:16, b:32, l:44};
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const t0 = pts[0].t, t1 = pts[pts.length - 1].t;
  const ys = pts.map(p => p.y);
  const yMin = Math.min(0, Math.min.apply(null, ys));
  const yMax = (Math.max.apply(null, ys) || 1) * 1.12;
  const xOf = t => m.l + (t1 === t0 ? iw / 2 : (t - t0) / (t1 - t0) * iw);
  const yOf = v => m.t + ih - ((v - yMin) / ((yMax - yMin) || 1)) * ih;
  const s = svg("svg", {class:"chart", viewBox:"0 0 " + W + " " + H, height:H});
  for(let i = 0; i <= 4; i++){
    const v = yMin + (yMax - yMin) * i / 4, y = yOf(v);
    s.appendChild(svg("line", {class:"grid-line", x1:m.l, x2:W - m.r, y1:y, y2:y}));
    const label = svg("text", {class:"tick", x:m.l - 6, y:y + 3, "text-anchor":"end"});
    label.textContent = String(+v.toFixed(2)); s.appendChild(label);
  }
  [0, 0.5, 1].forEach(f => {
    const t = t0 + (t1 - t0) * f;
    const label = svg("text", {class:"tick", x:xOf(t), y:H - 11, "text-anchor":"middle"});
    label.textContent = fmtShort(new Date(t).toISOString()); s.appendChild(label);
  });
  s.appendChild(svg("line", {class:"axis-line", x1:m.l, x2:W - m.r, y1:m.t + ih, y2:m.t + ih}));
  const d = pts.map((p, i) => (i ? "L" : "M") + xOf(p.t).toFixed(1) + " " + yOf(p.y).toFixed(1)).join(" ");
  s.appendChild(svg("path", {d:d, fill:"none", stroke:"var(--s1)", "stroke-width":2}));
  pts.forEach(p => {
    const c = svg("circle", {cx:xOf(p.t), cy:yOf(p.y), r:4.2, fill:"var(--s1)",
      stroke:"var(--card)", "stroke-width":2});
    c.addEventListener("mousemove", ev => showTip(
      '<div class="tt">' + esc(p.y) + "</div>" +
      '<div class="ts">' + esc(p.e.sname) + "</div>" +
      '<div class="ts">' + esc(p.e.by || "") + " &middot; " + esc(fmtDay(encDate(p.e))) + "</div>",
      ev.clientX, ev.clientY));
    c.addEventListener("mouseleave", hideTip);
    s.appendChild(c);
  });
  host.appendChild(s);
}

function barChart(host, items, opts){
  opts = opts || {};
  host.innerHTML = "";
  if(!items.length){ host.innerHTML = '<div class="empty">' + esc(opts.empty || "No data.") + "</div>"; return; }
  const rowH = 29, pad = 6, labelW = opts.labelW || 150;
  const W = host.clientWidth || 560, H = items.length * rowH + pad * 2;
  const barX = labelW + 8, iw = Math.max(40, W - barX - 54);
  const max = Math.max.apply(null, items.map(d => d.value)) || 1;
  const s = svg("svg", {class:"chart", viewBox:"0 0 " + W + " " + H, height:H});
  items.forEach((d, i) => {
    const y = pad + i * rowH;
    const label = svg("text", {class:"dlabel", x:labelW, y:y + rowH / 2 + 4, "text-anchor":"end"});
    label.textContent = d.label.length > 30 ? d.label.slice(0, 29) + "…" : d.label;
    s.appendChild(label);
    const w = Math.max(2, (d.value / max) * iw);
    const bar = svg("rect", {x:barX, y:y + 5, width:w, height:rowH - 14, rx:4,
      fill:d.color || "var(--s1)"});
    if(opts.onClick) bar.style.cursor = "pointer";
    bar.addEventListener("mousemove", ev => showTip(
      '<div class="tt">' + esc(d.value) + '</div><div class="ts">' + esc(d.label) + "</div>" +
      (opts.onClick ? '<div class="ts">click to filter</div>' : ""), ev.clientX, ev.clientY));
    bar.addEventListener("mouseleave", hideTip);
    if(opts.onClick) bar.addEventListener("click", () => { hideTip(); opts.onClick(d); });
    s.appendChild(bar);
    const value = svg("text", {class:"dlabel", x:barX + w + 6, y:y + rowH / 2 + 4});
    value.textContent = d.value; s.appendChild(value);
  });
  host.appendChild(s);
}

function renderTypes(rows){
  const counts = {};
  for(const e of rows) counts[e.type] = (counts[e.type] || 0) + 1;
  barChart($("#chartTypes"), Object.entries(counts).sort((a, b) => b[1] - a[1])
    .map(([label, value]) => ({label, value, color: typeColor[label]})),
    {empty: "No encounters match the filter."});
}

function renderSubjects(rows){
  const counts = {};
  for(const e of rows){
    const c = counts[e.sid] || (counts[e.sid] = {name: e.sname || "(unnamed)", value: 0});
    c.value++;
  }
  const arr = Object.entries(counts).map(([id, c]) => ({id, label: c.name, value: c.value}))
    .sort((a, b) => b.value - a.value);
  $("#subjHint").textContent = state.subject ? "filtered to 1 pump - Reset to clear"
    : (arr.length > 15 ? "top 15 of " + arr.length : arr.length + " pumps");
  barChart($("#chartSubjects"), arr.slice(0, 15), {labelW: 210,
    empty: "No pumps match the filter.",
    onClick: d => { state.subject = (state.subject === d.id) ? "" : d.id;
      $("#subjectSel").value = state.subject; render(); }});
}

function renderUsers(rows){
  const counts = {};
  for(const e of rows){ const u = e.by || "(none)"; counts[u] = (counts[u] || 0) + 1; }
  const arr = Object.entries(counts).map(([label, value]) => ({id: label, label, value}))
    .sort((a, b) => b.value - a.value);
  $("#userHint").textContent = state.user ? "filtered to 1 operator - Reset to clear"
    : (arr.length > 20 ? "top 20 of " + arr.length : arr.length + " operators");
  barChart($("#chartUsers"), arr.slice(0, 20), {labelW: 210,
    empty: "No submitter data in the current filter.",
    onClick: d => { const u = d.id === "(none)" ? "" : d.id;
      state.user = (state.user === u) ? "" : u; $("#userSel").value = state.user; render(); }});
}

function renderFreq(rows){
  const host = $("#chartFreq"), key = state.freq;
  if(!key){ host.innerHTML = '<div class="empty">No categorical observations.</div>'; return; }
  const counts = {};
  for(const e of rows) for(const o of e.obs){
    if(o.key !== key) continue;
    const values = o.list ? o.list : [o.value];
    for(const v of values){ const t = String(v).trim(); if(t) counts[t] = (counts[t] || 0) + 1; }
  }
  barChart(host, Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, 12)
    .map(([label, value]) => ({label, value, color: "var(--s2)"})),
    {labelW: 300, empty: "No answers for this concept in the current filter."});
}

/* ---- operators ---- */
function renderOperators(){
  $("#opBody").innerHTML = OPERATORS.map(o =>
    "<tr><td>" + esc(o.operator) + '</td><td class="num">' + o.n + '</td><td class="num">' + o.surveys +
    '</td><td class="num">' + o.subjects + '</td><td class="num">' + o.activeDays +
    '</td><td class="num">' + num(o.meanGap, 1) + '</td><td class="num">' + num(o.gapCv, 2) +
    '</td><td class="num">' + (o.onTime === null || o.onTime === undefined ? "-" :
      Math.round(o.onTime * 100) + "%") +
    '</td><td class="hide-sm num">' + esc(fmtShort(o.first)) + " - " + esc(fmtShort(o.last)) +
    "</td></tr>").join("") ||
    '<tr><td colspan="9" class="empty">No operator data.</td></tr>';
}

/* ---- encounter table ---- */
function obsValueHtml(o){
  const media = o.media || [];
  let out = "";
  if(o.list) out += o.list.filter(x => !media.includes(x))
    .map(x => '<span class="pill">' + esc(x) + "</span>").join("");
  else if(!media.length) out += esc(o.value);
  if(media.length){
    out += '<div class="thumbs">' + media.map(u =>
      '<a href="' + esc(u) + '" target="_blank" rel="noopener" title="' + esc(u) + '">' +
      '<img loading="lazy" src="' + esc(u) + '" alt="photo" ' +
      "onerror=\"this.replaceWith(Object.assign(document.createElement('span'),"+
      "{className:'pill',textContent:'photo (needs auth)'}))\"></a>").join("") + "</div>";
  }
  return out || "-";
}
function detailHtml(e){
  const rows = e.obs.map(o =>
    '<div class="o"><div class="k' + (o.derived ? " der" : "") + '">' + esc(o.key) +
    (o.col ? ' <span class="mono">' + esc(o.col) + "</span>" : "") + "</div>" +
    '<div class="v">' + obsValueHtml(o) + "</div></div>").join("");
  const meta =
    '<div class="o"><div class="k">Pump</div><div class="v">' + esc(e.sname) +
      ' <span class="mono">' + esc(e.sid) + "</span></div></div>" +
    '<div class="o"><div class="k">Submitted by</div><div class="v">' + esc(e.by || "-") +
      (e.mod && e.mod !== e.by ? ' <span class="sub">(last modified by ' + esc(e.mod) + ")</span>" : "") +
      (e.created && e.created !== e.by ? ' <span class="sub">(record created by ' + esc(e.created) + ")</span>" : "") +
      "</div></div>" +
    '<div class="o"><div class="k">Encounter ID</div><div class="v mono">' + esc(e.id) + "</div></div>" +
    (e.lag === null || e.lag === undefined ? "" :
      '<div class="o"><div class="k der">submission_lag_days</div><div class="v">' + num(e.lag, 2) +
      (e.onTime === null || e.onTime === undefined ? "" : (e.onTime ? " (on time)" : " (late)")) +
      "</div></div>");
  return '<div class="obs">' + (rows || '<div class="sub">(no observations)</div>') + meta + "</div>";
}
function sortRows(rows){
  const k = state.sort, dir = state.dir;
  return rows.slice().sort((a, b) => {
    let av, bv;
    if(k === "nobs"){ av = a.obs.length; bv = b.obs.length; }
    else if(k === "status"){ av = STATUS_ORDER[a.status]; bv = STATUS_ORDER[b.status]; }
    else { av = a[k] || ""; bv = b[k] || ""; }
    if(av < bv) return -dir;
    if(av > bv) return dir;
    return 0;
  });
}
function renderTable(rows){
  const body = $("#tbody");
  body.innerHTML = "";
  sortRows(rows).forEach(e => {
    const tr = document.createElement("tr");
    tr.className = "rowmain";
    tr.innerHTML =
      "<td>" + esc(e.sname || "-") + "</td>" +
      '<td><span class="tdot" style="background:' + typeColor[e.type] + '"></span>' + esc(e.type) +
        (e.ver ? ' <span class="mono">' + esc(e.ver) + "</span>" : "") + "</td>" +
      "<td>" + esc(e.by || "-") + "</td>" +
      '<td><span class="badge b-' + e.status + '">' + e.status + "</span>" +
        (e.flags || []).map(f => '<span class="flag">' + esc(f.replace(/_/g, " ")) + "</span>").join("") +
        "</td>" +
      '<td class="num hide-sm">' + esc(fmtDay(e.sched)) + "</td>" +
      '<td class="num">' + esc(fmtDay(e.comp)) + "</td>" +
      '<td class="num hide-sm">' + e.obs.length + "</td>";
    const detail = document.createElement("tr");
    detail.className = "detail";
    detail.style.display = "none";
    detail.innerHTML = '<td colspan="7">' + detailHtml(e) + "</td>";
    tr.addEventListener("click", () => {
      detail.style.display = detail.style.display === "none" ? "" : "none";
    });
    body.appendChild(tr);
    body.appendChild(detail);
  });
  $("#count").textContent = "- " + rows.length + " shown";
}

/* ---- quality + table index ---- */
function renderQuality(){
  const warnings = QUALITY.warnings || [];
  $("#qcWarnings").innerHTML = warnings.map(w => '<div class="warn">' + esc(w) + "</div>").join("");
  const attribution = QUALITY.attribution || {};
  const flags = QUALITY.flags || {};
  const rows = [
    ["Encounters pulled", attribution.pulled],
    ["Attributed by Subject ID", attribution.by_subject_id],
    ["Attributed via subject-doc fallback", attribution.by_subject_doc_fallback],
    ["Unattributed encounters", attribution.unattributed],
    ["Claimed by >1 subject", attribution.claimed_by_multiple_subjects],
    ["Duplicate encounter ids", (QUALITY.duplicate_encounter_ids || []).length],
    ["Distinct operators", QUALITY.distinct_operators],
    ["Grievances with a missing target survey", QUALITY.orphan_grievance_targets],
    ["Surveys missing a chlorine reading", QUALITY.surveys_missing_chlorine],
    ["Same pump, same day surveys", QUALITY.same_pump_same_day_surveys],
    ["Planned/cancelled excluded from facts", QUALITY.planned_or_cancelled_excluded_from_facts],
    ["Unmapped concepts", Object.keys(QUALITY.unmapped_concepts || {}).length],
    ["Flag: chlorine out of range", flags.chlorine_out_of_range],
    ["Flag: pump runtime implausible", flags.pump_runtime_implausible],
    ["Flag: refill timestamp in the future", flags.refill_in_future],
    ["Voided subjects / encounters",
      ((QUALITY.voided || {}).subjects || 0) + " / " + ((QUALITY.voided || {}).encounters || 0)],
  ];
  $("#qcGrid").innerHTML = rows.filter(r => r[1] !== undefined && r[1] !== null)
    .map(r => '<div class="qrow"><span>' + esc(r[0]) + "</span><b>" + esc(r[1]) + "</b></div>").join("");
}
function renderTableIndex(){
  $("#tblBody").innerHTML = (DATA.tables || []).map(t =>
    "<tr><td>" + esc(t.name) + '.csv</td><td class="num">' + t.rows + '</td><td class="num">' +
    t.cols + "</td><td>" + esc(t.grain || "") + "</td></tr>").join("");
}

/* ---- orchestration ---- */
function render(){
  const rows = filtered();
  renderHeader();
  renderKpis(rows);
  renderMetric(rows);
  renderTypes(rows);
  renderUsers(rows);
  renderSubjects(rows);
  renderFreq(rows);
  renderTable(rows);
}

function initFilters(){
  const subjectSel = $("#subjectSel");
  subjectSel.innerHTML = ['<option value="">All pumps (' + SUBJECTS.length + ")</option>"].concat(
    SUBJECTS.slice().sort((a, b) => String(a.name).localeCompare(String(b.name)))
      .map(s => '<option value="' + esc(s.id) + '">' + esc(s.name) + " (" + s.nEnc + ")</option>")
  ).join("");
  subjectSel.addEventListener("change", ev => { state.subject = ev.target.value; render(); });

  const counts = {};
  for(const e of ENCS) if(e.by) counts[e.by] = (counts[e.by] || 0) + 1;
  const userSel = $("#userSel");
  userSel.innerHTML = ['<option value="">All operators (' + USERS.length + ")</option>"].concat(
    USERS.slice().sort((a, b) => (counts[b] || 0) - (counts[a] || 0))
      .map(u => '<option value="' + esc(u) + '">' + esc(u) + " (" + (counts[u] || 0) + ")</option>")
  ).join("");
  userSel.addEventListener("change", ev => { state.user = ev.target.value; render(); });

  $("#typeChips").innerHTML = TYPES.map(t =>
    '<span class="tc" data-t="' + esc(t) + '"><span class="dot" style="background:' +
    typeColor[t] + '"></span>' + esc(t) + "</span>").join("");
  $("#typeChips").querySelectorAll(".tc").forEach(el => {
    el.addEventListener("click", () => {
      const t = el.dataset.t;
      if(state.types.has(t)){ state.types.delete(t); el.classList.add("off"); }
      else { state.types.add(t); el.classList.remove("off"); }
      render();
    });
  });

  const metricSel = $("#metricSel");
  metricSel.innerHTML = CATALOG.numeric.map(n =>
    "<option" + (n === state.metric ? " selected" : "") + ">" + esc(n) + "</option>").join("")
    || "<option>(none)</option>";
  metricSel.addEventListener("change", () => { state.metric = metricSel.value; render(); });

  const freqSel = $("#freqSel");
  freqSel.innerHTML = CATALOG.cat.map(n =>
    "<option" + (n === state.freq ? " selected" : "") + ">" + esc(n) + "</option>").join("")
    || "<option>(none)</option>";
  freqSel.addEventListener("change", () => { state.freq = freqSel.value; render(); });

  $("#q").addEventListener("input", ev => { state.q = ev.target.value; render(); });
  $("#statusSel").addEventListener("change", ev => { state.status = ev.target.value; render(); });
  $("#from").addEventListener("change", ev => { state.from = ev.target.value; render(); });
  $("#to").addEventListener("change", ev => { state.to = ev.target.value; render(); });
  $("#reset").addEventListener("click", () => {
    Object.assign(state, {q:"", status:"", from:"", to:"", subject:"", user:"", types:new Set(TYPES)});
    ["#q", "#statusSel", "#from", "#to", "#subjectSel", "#userSel"].forEach(sel => {
      const el = $(sel); if(el) el.value = "";
    });
    $("#typeChips").querySelectorAll(".tc").forEach(el => el.classList.remove("off"));
    render();
  });
  document.querySelectorAll("thead th[data-sort]").forEach(th => {
    th.addEventListener("click", () => {
      const k = th.dataset.sort;
      if(state.sort === k) state.dir *= -1;
      else { state.sort = k; state.dir = (k === "comp" || k === "sched") ? -1 : 1; }
      render();
    });
  });
}

function initTheme(){
  const root = document.documentElement;
  let saved = null;
  try { saved = localStorage.getItem("avni-report-theme"); } catch(err) { saved = null; }
  if(saved) root.setAttribute("data-theme", saved);
  $("#themeBtn").addEventListener("click", () => {
    const current = root.getAttribute("data-theme");
    const isDark = current ? current === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    const next = isDark ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem("avni-report-theme", next); } catch(err) { /* private mode */ }
    render();
  });
}

function initCsv(){
  $("#csv").addEventListener("click", () => {
    const rows = sortRows(filtered());
    const head = ["encounter_id", "subject_id", "subject_name", "subject_type", "encounter_type",
      "form_version", "status", "submitted_by", "scheduled_date", "completed_at",
      "submission_lag_days", "observations"];
    const quote = v => '"' + String(v == null ? "" : v).replace(/"/g, '""') + '"';
    const lines = [head.join(",")];
    for(const e of rows){
      const obs = e.obs.filter(o => !o.derived)
        .map(o => o.key + ": " + (o.list ? o.list.join("|") : o.value)).join(" ; ");
      lines.push([e.id, e.sid, e.sname, e.stype, e.type, e.ver, e.status, e.by, e.sched,
        e.comp, e.lag, obs].map(quote).join(","));
    }
    const blob = new Blob([lines.join("\n")], {type: "text/csv"});
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "avni_encounters_filtered.csv";
    a.click();
    URL.revokeObjectURL(a.href);
  });
}

$("#foot").textContent = "Generated " + GENERATED + " from " + SOURCE + " - " +
  SUBJECTS.length + " subjects, " + ENCS.length + " encounters, " + OPERATORS.length +
  " operators - self-contained, no external assets";
initFilters(); initTheme(); initCsv(); renderOperators(); renderQuality(); renderTableIndex();
render();
addEventListener("resize", () => {
  const rows = filtered();
  renderMetric(rows); renderTypes(rows); renderUsers(rows); renderSubjects(rows); renderFreq(rows);
});
</script>
</body>
</html>
"""
