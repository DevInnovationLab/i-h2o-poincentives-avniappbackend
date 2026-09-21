#!/usr/bin/env python3
"""
emit_csv.py
-----------
Writes (and reads back) the CSV deliverable: the analysis tables, the data
dictionary that documents every column of every table, a run manifest and a
data-quality report.

ENCODING RULES (the contract downstream code relies on; documented once here
and again in data_dictionary.csv):
  * booleans  -> 1 / 0            (models want numerics; dtype is 'bool01')
  * missing   -> empty cell       (never a sentinel like -1 or 0)
  * datetimes -> ISO 8601 UTC with a trailing Z
  * dates     -> ISO 8601 (yyyy-mm-dd)
  * lists     -> pipe-joined in a single cell
  * ids       -> strings, always (never coerced to numbers)
  * column order and row order are stable, so re-runs diff cleanly

``read_tables()`` is the inverse: it rebuilds typed rows from the CSVs using the
dtypes in data_dictionary.csv, which is what lets report.py be built *from the
CSV tables* rather than from in-memory state.

USAGE
-----
    from avni_pipeline import emit_csv

    paths  = emit_csv.write_all(tables, "1-data/12-avni/121-clean")
    tables = emit_csv.read_tables("1-data/12-avni/121-clean")   # round-trip, typed
"""

import csv
import datetime
import json
import os

from . import concepts, transform

ANALYSIS_TABLES = ["subjects", "operators", "encounters",
                   transform.SURVEY, transform.GRIEVANCE, transform.PAYMENT,
                   "observations_long"]
VOIDED_TABLES = ["subjects_voided", "encounters_voided"]
VOIDED_DIR = "_voided"
DICTIONARY_COLUMNS = ["table", "column", "dtype", "unit", "source_concept",
                      "description", "example"]


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #

def format_value(value):
    """One typed Python value -> its CSV cell, per the encoding rules above."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, datetime.datetime):
        return value.astimezone(datetime.timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z")
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, float):
        return "%.10g" % value
    if isinstance(value, (list, tuple)):
        return "|".join(format_value(v) for v in value)
    return str(value)


def write_table(table, path):
    """Write one Table to CSV in its declared column order."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(table.columns)
        for row in table.rows:
            writer.writerow([format_value(row.get(c)) for c in table.columns])
    return path


# --------------------------------------------------------------------------- #
# Write everything
# --------------------------------------------------------------------------- #

def write_all(tables, out_dir, big_table=False):
    """Write every table + data_dictionary.csv + _manifest.json + _quality_report.md.

    Returns the list of paths written (relative to ``out_dir``).
    """
    os.makedirs(out_dir, exist_ok=True)
    written = []

    for name in ANALYSIS_TABLES:
        if name in tables:
            written.append(write_table(tables[name], os.path.join(out_dir, f"{name}.csv")))
    for name in VOIDED_TABLES:
        if name in tables and tables[name].rows:
            written.append(write_table(tables[name],
                                       os.path.join(out_dir, VOIDED_DIR, f"{name}.csv")))

    if big_table:
        written.append(write_table(build_big_table(tables),
                                   os.path.join(out_dir, "encounters_wide.csv")))

    written.append(write_dictionary(tables, os.path.join(out_dir, "data_dictionary.csv")))
    written.append(write_manifest(tables, out_dir, written))
    written.append(write_quality_report(tables, os.path.join(out_dir, "_quality_report.md")))
    return written


def write_dictionary(tables, path):
    """Every column of every table: name, dtype, unit, source concept, example."""
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=DICTIONARY_COLUMNS,
                                extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for entry in tables.dictionary:
            writer.writerow(entry)
    return path


def write_manifest(tables, out_dir, written):
    """Run metadata: where the data came from, options used, counts, QC summary."""
    path = os.path.join(out_dir, "_manifest.json")
    manifest = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc)
                        .isoformat(timespec="seconds").replace("+00:00", "Z"),
        "pipeline_version": _pipeline_version(),
        "source": dict(tables.meta),
        "encoding": {"booleans": "1/0", "missing": "empty cell",
                     "datetimes": "ISO 8601 UTC (Z)", "dates": "ISO 8601",
                     "lists": "pipe-joined", "ids": "string"},
        "factor_levels": concepts.FACTOR_LEVELS,
        "tables": {name: {"rows": len(table.rows), "columns": len(table.columns),
                          "grain": table.grain}
                   for name, table in tables.tables.items()},
        "files": sorted(os.path.relpath(p, out_dir) for p in written if p),
        "quality": tables.quality,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, ensure_ascii=False, default=str)
    return path


def write_quality_report(tables, path):
    """Human-readable QC summary (the machine-readable copy lives in the manifest)."""
    q = tables.quality
    out = ["# Data quality report", "",
           f"Source: `{tables.meta.get('source')}` · "
           f"generated {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}", ""]

    out += ["## Row counts", "", "| table | rows |", "|---|---|"]
    for name, count in q.get("row_counts", {}).items():
        out.append(f"| {name} | {count} |")

    attribution = q.get("attribution") or {}
    if attribution:
        out += ["", "## Encounter attribution (client-side Subject-ID join)", ""]
        for key in ("pulled", "attributed", "by_subject_id", "by_subject_doc_fallback",
                    "unattributed", "claimed_by_multiple_subjects"):
            if key in attribution:
                out.append(f"- **{key}**: {attribution[key]}")

    out += ["", "## Domain flags (rows kept and flagged, never dropped)", ""]
    for key, count in (q.get("flags") or {}).items():
        out.append(f"- {key}: {count}")

    out += ["", "## Coverage", "",
            f"- distinct submitting users: {q.get('distinct_operators')}",
            f"- encounters with no subject link: {q.get('encounters_without_subject')}",
            f"- duplicate encounter ids: {len(q.get('duplicate_encounter_ids') or [])}",
            f"- grievances pointing at a missing survey: {q.get('orphan_grievance_targets')}",
            f"- surveys missing a chlorine reading: {q.get('surveys_missing_chlorine')}",
            f"- planned/cancelled encounters excluded from facts: "
            f"{q.get('planned_or_cancelled_excluded_from_facts')}"]

    unmapped = q.get("unmapped_concepts") or {}
    out += ["", "## Unmapped concepts (still in observations_long.csv)", ""]
    out += ([f"- `{name}` ({count} obs)" for name, count in unmapped.items()]
            if unmapped else ["- none: every concept has a column."])

    unmapped_answers = q.get("unmapped_multiselect_answers") or {}
    if unmapped_answers:
        out += ["", "## Multi-select answers outside the curated map", ""]
        for concept_name, answers in unmapped_answers.items():
            for answer in answers:
                out.append(f"- `{concept_name}` = {answer}")

    warnings = q.get("warnings") or []
    out += ["", "## Warnings", ""]
    out += ([f"- {w}" for w in warnings] if warnings else ["- none."])

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    return path


def build_big_table(tables):
    """Option B convenience export: one row per encounter, one column per concept."""
    long_rows = tables["observations_long"].rows
    wide = {}
    concept_columns = []
    for row in long_rows:
        key = row["encounter_id"]
        column = row["concept_column"] or f"obs_{concepts.slug(row['concept_name'])}"
        if column not in concept_columns:
            concept_columns.append(column)
        cell = wide.setdefault(key, {})
        cell[column] = (f"{cell[column]}|{row['value_text']}"
                        if column in cell else row["value_text"])
    base = tables["encounters"]
    columns = list(base.columns) + sorted(concept_columns)
    rows = []
    for enc in base.rows:
        row = dict(enc)
        row.update(wide.get(enc["encounter_id"], {}))
        rows.append(row)
    return transform.Table("encounters_wide", columns, rows,
                           grain="1 row per encounter, every concept as a column")


def _pipeline_version():
    from . import __version__
    return __version__


# --------------------------------------------------------------------------- #
# Read back (typed) -- lets report.py build from the CSVs on disk
# --------------------------------------------------------------------------- #

def _parse_cell(text, dtype):
    if text == "":
        return None
    if dtype == "int":
        try:
            return int(text)
        except ValueError:
            return None
    if dtype == "float":
        try:
            return float(text)
        except ValueError:
            return None
    if dtype == "bool01":
        return text.strip() in ("1", "true", "True")
    if dtype == "date":
        return concepts.parse_date(text)
    if dtype == "datetime":
        return concepts.parse_datetime(text)
    return text


def read_tables(out_dir):
    """Rebuild a TableSet from a directory written by write_all().

    Types come from data_dictionary.csv; meta/quality come from _manifest.json.
    Columns with no dictionary entry are left as strings.
    """
    dictionary, dtypes = [], {}
    dict_path = os.path.join(out_dir, "data_dictionary.csv")
    if os.path.exists(dict_path):
        with open(dict_path, encoding="utf-8", newline="") as fh:
            for entry in csv.DictReader(fh):
                dictionary.append(entry)
                dtypes[(entry["table"], entry["column"])] = entry["dtype"]

    manifest = {}
    manifest_path = os.path.join(out_dir, "_manifest.json")
    if os.path.exists(manifest_path):
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)

    tables = transform.TableSet(meta=manifest.get("source") or {})
    tables.dictionary = dictionary
    tables.quality = manifest.get("quality") or {}
    tables.meta.setdefault("generated_at", manifest.get("generated_at"))

    for name in ANALYSIS_TABLES + VOIDED_TABLES:
        path = os.path.join(out_dir, f"{name}.csv")
        if name in VOIDED_TABLES:
            path = os.path.join(out_dir, VOIDED_DIR, f"{name}.csv")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8", newline="") as fh:
            reader = csv.reader(fh)
            columns = next(reader, [])
            rows = [{c: _parse_cell(v, dtypes.get((name, c), "string"))
                     for c, v in zip(columns, values)} for values in reader]
        grain = ((manifest.get("tables") or {}).get(name) or {}).get("grain", "")
        tables.tables[name] = transform.Table(name, columns, rows, grain=grain)
    return tables
