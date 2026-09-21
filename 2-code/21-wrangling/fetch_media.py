#!/usr/bin/env python3
"""
fetch_media.py
--------------
Localise the field photos for an output directory produced by avni_to_tables.py,
so the HTML report renders without credentials and the folder can be shared.

    <out>/*.csv            reference  https://s3.../prod-user-media/<uuid>.jpg
        |
        |  GET /media/signedUrl  (authenticated)  ->  presigned URL  ->  bytes
        v
    <out>/media/<uuid>.jpg            the downloaded files
    <out>/media/_media_manifest.csv   s3_url -> local_path, bytes, sha256
    <out>/report.html                 rewritten to point at media/<uuid>.jpg

The CSVs are left alone: the S3 URL is the stable identifier and the manifest is
the join back to the local copy. Only the report is rewritten, because only the
report needs to render offline.

Safe to re-run: existing non-empty files are skipped, so a second pass only
fetches what failed the first time.

USAGE
-----
    export AVNI_SERVER="https://app.avniproject.org"
    export AVNI_AUTH_TOKEN="<cognito id token>"

    # localise the photos for an output directory
    python3 2-code/21-wrangling/fetch_media.py --dir 1-data/12-avni/121-clean

    # a folder on Box Drive works the same way
    python3 2-code/21-wrangling/fetch_media.py --dir ~/Library/CloudStorage/Box-Box/12-avni/121-clean

    # see what would be fetched, without downloading
    python3 2-code/21-wrangling/fetch_media.py --dir 1-data/12-avni/121-clean --dry-run

Options: --workers (parallel downloads, default 6), --media-subdir (default
"media"), --timeout, --no-rewrite (download but leave report.html pointing at
S3), --quiet.
"""

import argparse
import os
import sys

# The importable code lives one directory over, in 2-code/20-programs/ --
# avni_auth.py and the avni_pipeline package. The CLIs in 21-wrangling/ are the
# only things that know that layout, so put it on the path here.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "20-programs"))

from avni_pipeline import media                      # noqa: E402


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Download Avni field photos into <dir>/media and point the "
                    "HTML report at them.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--dir", required=True,
                   help="output directory written by avni_to_tables.py")
    p.add_argument("--media-subdir", default="media",
                   help="subdirectory to store the downloaded files in")
    p.add_argument("--report", default=None,
                   help="report to rewrite (default: <dir>/report.html)")
    p.add_argument("--workers", type=int, default=media.DEFAULT_WORKERS,
                   help="parallel downloads")
    p.add_argument("--timeout", type=int, default=30, help="per-request timeout (s)")
    p.add_argument("--dry-run", action="store_true",
                   help="list the media URLs and exit without downloading")
    p.add_argument("--no-rewrite", dest="rewrite", action="store_false",
                   help="download the files but leave report.html pointing at S3")
    p.add_argument("--quiet", action="store_true", help="less chatter")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    log = (lambda *_: None) if args.quiet else print

    out_dir = os.path.expanduser(args.dir)
    if not os.path.isdir(out_dir):
        sys.exit(f"ERROR: not a directory: {out_dir}")

    urls = media.collect_urls(out_dir)
    log(f"Found {len(urls)} media URL(s) referenced by the tables in {out_dir}/")
    if not urls:
        return 0
    if args.dry_run:
        for url in urls:
            log("  " + url)
        return 0

    from avni_pipeline import pull                   # imports avni_auth
    from avni_auth import AvniError

    media_dir = os.path.join(out_dir, args.media_subdir)
    try:
        client = pull.make_client()
        result = media.download_all(client, urls, media_dir,
                                    workers=args.workers, timeout=args.timeout,
                                    progress=log)
    except AvniError as err:
        sys.exit(f"ERROR talking to Avni:\n{err}\n\n"
                 "See 4-documentation/40-data-sources/avni-api.md > Authentication: "
                 "401 means the id token expired.")

    log(f"\n{result.summary()}")
    manifest = media.write_manifest(result, media_dir)
    log(f"Manifest -> {manifest}")

    if args.rewrite:
        report = args.report or os.path.join(out_dir, "report.html")
        replaced = media.rewrite_report(report, result.local_by_url)
        if replaced:
            log(f"Rewrote {replaced} URL(s) in {report} -> {args.media_subdir}/")
        elif os.path.exists(report):
            log(f"No URLs rewritten in {report} (already localised?)")

    if result.failed:
        from collections import Counter
        reasons = Counter(result.failed.values())
        log(f"\n  ! {len(result.failed)} of {len(urls)} file(s) could not be fetched:")
        for reason, count in reasons.most_common():
            log(f"      {count:>4}  {reason}")
        if media.MISSING_FROM_BUCKET in reasons:
            log("\n    'missing from bucket' means the observation references a photo\n"
                "    that is not in S3 -- an upload that never completed or was purged.\n"
                "    Re-running will not recover these; they are listed in "
                f"{args.media_subdir}/{media.MANIFEST_NAME}.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
