#!/usr/bin/env python3
"""
avni_to_tables.py
-----------------
Entry point for the pipeline: Avni API -> tidy CSV tables -> one self-contained
interactive HTML report.

    API  --pull.py-->  raw records  --transform.py-->  tables
                                                        |--> emit_csv.py  -> *.csv
                                                        `--> report.py    -> report.html

Everything lands in --out, which defaults to 1-data/12-avni/121-clean.

Three input sources, same downstream code for all of them:

    api        live pull via avni_auth.AvniClient (needs AVNI_AUTH_TOKEN, or
               AVNI_USERNAME/AVNI_PASSWORD + boto3 for Cognito login)
    raw        replay a JSON snapshot written earlier with --save-raw
    csv        skip the data build entirely and just re-render the HTML report
               from an existing output directory

USAGE
-----
Run it from anywhere: --out defaults to the repo's 1-data/12-avni/121-clean and
is resolved from this file's location, not from the working directory.

    # live pull -> CSVs + report
    export AVNI_SERVER="https://app.avniproject.org"
    export AVNI_AUTH_TOKEN="<cognito id token>"        # or AVNI_USERNAME/AVNI_PASSWORD
    python3 2-code/21-wrangling/avni_to_tables.py --subject-type Pump

    # snapshot a pull, then rebuild tables from it without hitting the API again
    python3 2-code/21-wrangling/avni_to_tables.py --save-raw 1-data/12-avni/120-raw/_raw.json
    python3 2-code/21-wrangling/avni_to_tables.py --source raw --input 1-data/12-avni/120-raw/_raw.json

    # just rebuild the HTML from CSVs that already exist
    python3 2-code/21-wrangling/avni_to_tables.py --source csv --input 1-data/12-avni/121-clean

    # any of the above, plus localise the field photos into <out>/media/
    python3 2-code/21-wrangling/avni_to_tables.py --fetch-media

Options worth knowing: --include-planned (keep scheduled-only encounters in the
fact tables), --no-onehot (pipe-delimited multi-selects only), --big-table
(also write encounters_wide.csv), --tz (local timezone for calendar features),
--grace-days (on-time window), --chlorine-band LOW,HIGH (adds chlorine_in_target),
--fetch-media (download the referenced photos and point the report at the local
copies -- the same work as fetch_media.py next to this file, run automatically
at the end).
"""

import argparse
import os
import sys

# The importable code lives one directory over, in 2-code/20-programs/ --
# avni_auth.py and the avni_pipeline package. The CLIs in 21-wrangling/ are the
# only things that know that layout, so put it on the path here.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "20-programs"))

from avni_pipeline import emit_csv, media, report, transform  # noqa: E402

# --out used to default to the literal "data", which resolved against whatever
# directory the caller happened to be standing in and scattered stray data/
# folders. Anchor it to the repo instead: 21-wrangling/ -> up two -> repo root.
DEFAULT_OUT = os.path.normpath(
    os.path.join(HERE, os.pardir, os.pardir, "1-data", "12-avni", "121-clean"))

# --source raw with no --input used to look for _raw.json inside --out, because
# before the move the snapshot and the tables shared one "data/" folder. They no
# longer do: a snapshot is append-only evidence in 120-raw, the tables are
# derived and overwritten in 121-clean. Point the fallback at the snapshot.
DEFAULT_RAW = os.path.normpath(
    os.path.join(HERE, os.pardir, os.pardir,
                 "1-data", "12-avni", "120-raw", "_raw.json"))


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Avni -> tidy CSV tables -> self-contained HTML report.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--source", choices=("api", "raw", "csv"), default="api",
                   help="where the data comes from")
    p.add_argument("--input", default=None,
                   help="raw JSON snapshot / existing CSV dir "
                        "(depends on --source)")
    p.add_argument("--out", default=DEFAULT_OUT,
                   help="output directory for the CSV tables")
    p.add_argument("--subject-type", default=None,
                   help="filter the pull to one subject type, e.g. Pump (api only)")
    p.add_argument("--since", default=None,
                   help="incremental cursor, e.g. 2026-01-01T00:00:00.000Z (api only)")
    p.add_argument("--save-raw", default=None,
                   help="also write the raw API payloads to this JSON file (api only)")
    p.add_argument("--tz", default="Asia/Kolkata",
                   help="local timezone for calendar/derived features")
    p.add_argument("--grace-days", type=float, default=0.0,
                   help="days after the scheduled date that still count as on time")
    p.add_argument("--chlorine-band", default=None, metavar="LOW,HIGH",
                   help="add chlorine_in_target for this mg/L band, e.g. 0.2,2.0")
    p.add_argument("--include-planned", action="store_true",
                   help="keep scheduled-only encounters in the fact tables")
    p.add_argument("--no-onehot", dest="onehot", action="store_false",
                   help="skip multi-select indicator columns (keep the *_raw columns)")
    p.add_argument("--big-table", action="store_true",
                   help="also write encounters_wide.csv (one row per encounter, all concepts)")
    p.add_argument("--report", default=None,
                   help="HTML report path (default: <out>/report.html)")
    p.add_argument("--no-report", dest="want_report", action="store_false",
                   help="skip the HTML report")
    p.add_argument("--fetch-media", action="store_true",
                   help="download the field photos into <out>/media and point the "
                        "report at them (same as running fetch_media.py after)")
    p.add_argument("--media-workers", type=int, default=media.DEFAULT_WORKERS,
                   help="parallel media downloads (--fetch-media only)")
    p.add_argument("--media-subdir", default="media",
                   help="subdirectory for the downloaded files (--fetch-media only)")
    p.add_argument("--quiet", action="store_true", help="less chatter")
    return p.parse_args(argv)


def load_raw_dataset(args, log):
    """Return the raw dataset for the chosen source (or None for --source csv)."""
    if args.source == "raw":
        from avni_pipeline import pull
        path = args.input or DEFAULT_RAW
        log(f"Replaying raw snapshot {path}")
        return pull.load_raw(path)
    # ---- live API ----
    from avni_pipeline import pull                       # imports avni_auth (needs boto3
    from avni_auth import AvniError                      # only for the Cognito path)
    client = pull.make_client()
    log(f"Server {client.server} (api version {client.version})")
    try:
        raw = pull.pull_all(client, subject_type=args.subject_type,
                            since=args.since, progress=log)
    except AvniError as err:
        sys.exit(f"ERROR talking to Avni:\n{err}\n\n"
                 "See 4-documentation/40-data-sources/avni-api.md > Authentication: "
                 "a 500 is almost always the token (access-vs-id or wrong "
                 "environment); 401 means expired/missing.")
    if args.save_raw:
        pull.save_raw(raw, args.save_raw)
        log(f"Wrote raw snapshot -> {args.save_raw}")
    return raw


def fetch_media_step(args, out_dir, report_path, log):
    """Localise the photos the tables only reference. Returns an exit code.

    Deliberately the LAST thing a run does, and not part of the build: it reads
    the CSVs that emit_csv wrote (that is where the URLs are, whatever the
    source was) and edits the report file that report.py wrote. Both have to be
    on disk already.

    It also never aborts the run. A pull can take minutes and the id token
    expires in about an hour, so an auth failure at this point is routine --
    losing an hour of already-written tables to one would not be.
    """
    urls = media.collect_urls(out_dir)
    log(f"\nMedia: {len(urls)} photo(s) referenced by the tables")
    if not urls:
        return 0

    from avni_pipeline import pull                   # imports avni_auth
    from avni_auth import AvniError

    media_dir = os.path.join(out_dir, args.media_subdir)
    try:
        client = pull.make_client()
        client.token()          # surface a bad/absent token once here, rather
                                # than as one failure per file below
        result = media.download_all(client, urls, media_dir,
                                    workers=args.media_workers, progress=log)
    except AvniError as err:
        log(f"  ! media fetch failed: {err}")
        log(f"  ! the tables and report in {out_dir}/ are written and intact "
            "(the report keeps its S3 links).")
        log(f"  ! retry the photos alone with: "
            f"python3 2-code/21-wrangling/fetch_media.py --dir {out_dir}")
        return 1

    log(f"  {result.summary()}")
    log(f"  manifest -> {media.write_manifest(result, media_dir)}")
    replaced = media.rewrite_report(report_path, result.local_by_url)
    if replaced:
        log(f"  rewrote {replaced} URL(s) in {report_path} "
            f"-> {args.media_subdir}/")

    if result.failed:
        from collections import Counter
        log(f"  ! {len(result.failed)} of {len(urls)} photo(s) could not be fetched:")
        for reason, count in Counter(result.failed.values()).most_common():
            log(f"      {count:>4}  {reason}")
        if media.MISSING_FROM_BUCKET in result.failed.values():
            log("  ! 'missing from bucket' is a finding about the data, not a bug: "
                "the observation references a photo that is not in S3. "
                f"They are listed in {args.media_subdir}/{media.MANIFEST_NAME}.")
        return 1
    return 0


def main(argv=None):
    args = parse_args(argv)
    log = (lambda *_: None) if args.quiet else print

    band = None
    if args.chlorine_band:
        try:
            low, high = (float(x) for x in args.chlorine_band.split(","))
            band = (low, high)
        except ValueError:
            sys.exit("--chlorine-band expects LOW,HIGH, e.g. 0.2,2.0")

    # --source csv reads and re-renders in place; every other source writes to
    # --out. One variable, because the report and the media both have to land
    # next to the CSVs they describe.
    out_dir = (args.input or args.out) if args.source == "csv" else args.out

    if args.source == "csv":
        tables = emit_csv.read_tables(out_dir)
        if not tables.tables:
            sys.exit(f"ERROR: no CSV tables found in {out_dir}/")
        log(f"Read {len(tables.tables)} table(s) from {out_dir}/")
    else:
        raw = load_raw_dataset(args, log)
        tables = transform.build_tables(
            raw, tz=args.tz, include_planned=args.include_planned,
            onehot=args.onehot, grace_days=args.grace_days, chlorine_band=band)
        written = emit_csv.write_all(tables, out_dir, big_table=args.big_table)
        log(f"\nWrote {len(written)} file(s) to {out_dir}/")
        for name, count in tables.row_counts().items():
            if count or not name.endswith("_voided"):
                log(f"    {count:>6}  {name}")

    report_path = args.report or os.path.join(out_dir, "report.html")
    if args.want_report:
        report.write_report(tables, report_path)
        log(f"\nInteractive report -> {report_path}")

    warnings = (tables.quality or {}).get("warnings") or []
    for warning in warnings:
        log(f"  ! {warning}")

    # Last, and the only thing that can change the exit code: by now the CSVs
    # and the report are on disk no matter what happens next.
    if args.fetch_media:
        return fetch_media_step(args, out_dir, report_path, log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
