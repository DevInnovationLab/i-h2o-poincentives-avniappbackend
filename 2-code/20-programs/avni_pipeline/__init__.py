#!/usr/bin/env python3
"""
avni_pipeline
-------------
Avni API -> tidy CSV tables -> self-contained interactive HTML report.

The package is deliberately layered so the analysis half can be developed and
tested without network access -- replay a saved snapshot instead of pulling:

    concepts.py    concept-name -> column mapping, dtypes, value coercion
    pull.py        API reads via avni_auth.AvniClient (subjects/encounters/programs),
                   plus save_raw/load_raw: the offline replay path
    transform.py   raw records -> the tidy table set (+ derived features, QC)
    emit_csv.py    write/read the CSV tables, data dictionary and manifest
    report.py      table set -> one self-contained interactive HTML file

A replayed snapshot is the pull verbatim, so it has the SAME "raw dataset" shape
(Avni-shaped dicts) and `transform`/`emit_csv`/`report` are identical on either
path -- which is what makes the whole analysis half testable without the server.

USAGE
-----
    from avni_pipeline import pull, transform, emit_csv, report

    raw    = pull.load_raw("1-data/12-avni/120-raw/_raw.json")
    tables = transform.build_tables(raw)
    emit_csv.write_all(tables, "1-data/12-avni/121-clean")
    report.write_report(tables, "1-data/12-avni/121-clean/report.html")

Entry point: ../../21-wrangling/avni_to_tables.py (CLI wiring for every path).
"""

__version__ = "1.0.0"
