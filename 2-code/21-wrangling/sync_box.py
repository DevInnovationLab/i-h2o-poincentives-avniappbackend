#!/usr/bin/env python3
"""
sync_box.py
-----------
Publish an output directory produced by avni_to_tables.py to Box Drive, so the
team reads the tables and the report from Box rather than from someone's laptop.

    <raw>.json                            ->  12-avni/120-raw/pull_<UTC>.json
    <dir>/*.csv  data_dictionary.csv
          _manifest.json  _quality_report.md
          report.html  media/             ->  12-avni/121-clean/

WHY THIS EXISTS (and why it is a separate script)
-------------------------------------------------
Box Drive is a synced folder, not an API, so publishing is a file copy. Keeping
that here instead of in ``avni_pipeline/`` keeps the package transport-agnostic:
the pipeline writes a local directory and does not know, or care, where it is
published afterwards. Swapping Box for S3, a share drive or a data repo is then
a second standalone script rather than an edit to emit_csv.py.

The two destinations follow deliberately different rules:

    120-raw     append-only. A pull can never be reproduced -- the API only ever
                shows "now" -- so a snapshot is evidence and is never
                overwritten. Names carry a UTC stamp so the history sorts
                chronologically and two pulls can never collide silently.
    121-clean   overwritten every run. Everything in it is derived from a raw
                snapshot by code, so it is reproducible; the _manifest.json it
                contains records which pull and which options produced it.

Every copy is written to a ``.part`` name *inside the destination directory* and
then renamed into place: Box uploads whatever appears in the folder, and a
half-written report.html would be synced and shared as-is. The temp file is a
sibling of the destination, so the rename is same-filesystem even on the mount.
Sizes are compared afterwards because a network mount can accept a write and
still fail to persist it.

USAGE
-----
    # publish tables + report + media, and archive the snapshot they came from
    python3 2-code/21-wrangling/sync_box.py --dir 1-data/12-avni/121-clean --raw 1-data/12-avni/120-raw/_raw.json

    # show the plan, touch nothing
    python3 2-code/21-wrangling/sync_box.py --dir 1-data/12-avni/121-clean --dry-run

    # somewhere else entirely (a test mount, a second Box account)
    python3 2-code/21-wrangling/sync_box.py --dir 1-data/12-avni/121-clean --box-root /tmp/fakebox
    AVNI_BOX_ROOT=/tmp/fakebox python3 2-code/21-wrangling/sync_box.py --dir 1-data/12-avni/121-clean

Options: --raw-subdir (default 120-raw), --clean-subdir (default 121-clean),
--quiet.
"""

import argparse
import datetime
import json
import os
import re
import shutil
import sys

DEFAULT_BOX_ROOT = os.environ.get("AVNI_BOX_ROOT",
                                  "~/Library/CloudStorage/Box-Box/12-avni")

RAW_STAMP_FMT = "%Y%m%dT%H%M%SZ"

# A snapshot that already carries a UTC stamp keeps its name: re-stamping it
# would invent a second identity for one pull.
RAW_NAME_RE = re.compile(r"^pull_\d{8}T\d{6}Z\.json$", re.I)

# --save-raw often leaves a snapshot inside the output directory. It belongs in
# 120-raw (via --raw), not in the folder that gets overwritten every run.
RAW_IN_OUT_RE = re.compile(r"^(_raw\.json|pull_\d{8}T\d{6}Z\.json)$", re.I)

# Half-written files from an interrupted copy/download, ours or the pipeline's.
SKIP_SUFFIXES = (".part",)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Copy a pipeline output directory (and its raw snapshot) "
                    "to the Box Drive folders the team reads.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--dir", required=True,
                   help="local output directory written by avni_to_tables.py")
    p.add_argument("--raw", default=None,
                   help="raw JSON snapshot to archive (optional)")
    p.add_argument("--box-root", default=DEFAULT_BOX_ROOT,
                   help="Box folder holding the subfolders below "
                        "(env AVNI_BOX_ROOT overrides this default)")
    p.add_argument("--raw-subdir", default="120-raw",
                   help="append-only snapshot history, under --box-root")
    p.add_argument("--clean-subdir", default="121-clean",
                   help="published tables/report, under --box-root")
    p.add_argument("--dry-run", action="store_true",
                   help="print the planned copies and exit without writing")
    p.add_argument("--quiet", action="store_true", help="less chatter")
    return p.parse_args(argv)


# --------------------------------------------------------------------------- #
# Naming
# --------------------------------------------------------------------------- #

def _utc_stamp(when):
    return when.astimezone(datetime.timezone.utc).strftime(RAW_STAMP_FMT)


def raw_dest_name(raw_path, warn=None):
    """The ``pull_<UTC>.json`` name this snapshot should be archived under.

    Preference order: a name that is already stamped, then ``meta.pulled_at``
    from inside the file, then the file's mtime. pulled_at is preferred over
    mtime because it is when the DATA was read; the file can be copied, moved or
    re-saved long afterwards, and the history is indexed by the pull.

    The mtime fallback is silent for a snapshot that simply has no ``meta``, but
    a file that could not be READ or PARSED is reported through ``warn``: it is
    very likely truncated or corrupt, and filing it into an append-only archive
    under a name derived from when it happened to be touched would bury that.
    """
    base = os.path.basename(raw_path)
    if RAW_NAME_RE.match(base):
        return base

    stamp = ""
    try:
        with open(raw_path, encoding="utf-8") as fh:
            pulled = ((json.load(fh) or {}).get("meta") or {}).get("pulled_at")
        if pulled:
            # fromisoformat only learned "Z" in 3.11; normalise it by hand.
            when = datetime.datetime.fromisoformat(
                str(pulled).strip().replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=datetime.timezone.utc)
            stamp = _utc_stamp(when)
    except Exception as err:                   # noqa: BLE001 - never block a publish
        if warn:
            warn(f"  ! {base}: could not read meta.pulled_at "
                 f"({type(err).__name__}: {str(err)[:80]}) -- naming the archive "
                 "from the file's mtime instead. Check the snapshot is complete.")
        stamp = ""
    if not stamp:
        stamp = _utc_stamp(datetime.datetime.fromtimestamp(
            os.path.getmtime(raw_path), datetime.timezone.utc))
    return f"pull_{stamp}.json"


def human(nbytes):
    if nbytes >= 1e6:
        return f"{nbytes / 1e6:.1f} MB"
    return f"{nbytes / 1e3:.1f} KB"


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #

def plan_clean(src_dir, dest_dir):
    """``([(src, dest, rel, group)], [skipped_name])`` for the clean publication.

    Everything in the directory is published, rather than a hardcoded file list:
    the set of tables changes as forms change, and a table that exists locally
    but silently never reaches Box is the worst failure mode here.
    """
    jobs, skipped = [], []
    for name in sorted(os.listdir(src_dir)):
        path = os.path.join(src_dir, name)
        if name.startswith(".") or name.endswith(SKIP_SUFFIXES):
            continue
        if os.path.isfile(path):
            if RAW_IN_OUT_RE.match(name):
                skipped.append(name)
                continue
            jobs.append((path, os.path.join(dest_dir, name), name, name))
        elif os.path.isdir(path):              # media/ and anything like it
            for root, dirs, files in os.walk(path):
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                for fname in sorted(files):
                    if fname.startswith(".") or fname.endswith(SKIP_SUFFIXES):
                        continue
                    full = os.path.join(root, fname)
                    rel = os.path.relpath(full, src_dir)
                    # One console line per top-level folder: a media directory
                    # of several hundred photos should not bury the tables.
                    jobs.append((full, os.path.join(dest_dir, rel), rel,
                                 name + os.sep))
    return jobs, skipped


# --------------------------------------------------------------------------- #
# Copying
# --------------------------------------------------------------------------- #

def copy_one(src, dest):
    """Copy via a sibling ``.part`` file, then rename. Returns ``(bytes, error)``."""
    parent = os.path.dirname(dest)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = dest + ".part"
    try:
        shutil.copyfile(src, tmp)
        try:
            shutil.copystat(src, tmp)          # keep mtimes; best effort on a mount
        except OSError:
            pass
        os.replace(tmp, dest)                  # same dir -> same filesystem
    except OSError as err:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return 0, f"{type(err).__name__}: {err}"

    written, expected = os.path.getsize(dest), os.path.getsize(src)
    if written != expected:
        return written, f"size mismatch: {written} B on Box vs {expected} B local"
    return written, ""


def run_jobs(jobs, log, dry_run):
    """Execute (or narrate) the copies. Returns ``(nfiles, nbytes, failures)``.

    Files are grouped for the console by their top-level component, so a media
    folder of a few hundred photos is one line rather than a few hundred.
    """
    groups, failures = {}, []
    for src, dest, rel, group in jobs:
        entry = groups.setdefault(group, [0, 0])
        if dry_run:
            entry[0] += 1
            entry[1] += os.path.getsize(src)
            continue
        nbytes, err = copy_one(src, dest)
        if err:
            failures.append((rel, err))
            log(f"  ! {rel}: {err}")
            continue
        entry[0] += 1
        entry[1] += nbytes

    total_files = total_bytes = 0
    for key, (count, nbytes) in groups.items():
        if not count:                          # every file in this group failed
            continue
        label = f"{key}  ({count} file{'s' * (count != 1)})" \
            if key.endswith(os.sep) else key
        log(f"  {'~' if dry_run else '+'} {label:<44}{human(nbytes):>10}")
        total_files += count
        total_bytes += nbytes
    return total_files, total_bytes, failures


def main(argv=None):
    args = parse_args(argv)
    log = (lambda *_: None) if args.quiet else print

    src_dir = os.path.expanduser(args.dir)
    if not os.path.isdir(src_dir):
        sys.exit(f"ERROR: not a directory: {src_dir}")

    box_root = os.path.expanduser(args.box_root)
    raw_dir = os.path.join(box_root, args.raw_subdir)
    clean_dir = os.path.join(box_root, args.clean_subdir)

    missing = [d for d in ([box_root, clean_dir] + ([raw_dir] if args.raw else []))
               if not os.path.isdir(d)]
    if missing:
        sys.exit("ERROR: Box folder(s) not found:\n"
                 + "".join(f"    {d}\n" for d in missing)
                 + "\nCheck Box Drive is running and this folder is synced "
                   "locally (an unsynced folder simply does not exist on disk).\n"
                   "Use --box-root, or set AVNI_BOX_ROOT, to publish elsewhere.")

    if not os.path.exists(os.path.join(src_dir, "_manifest.json")):
        log(f"  ! no _manifest.json in {src_dir} -- is this a pipeline output "
            "directory? Publishing it anyway.")

    log(f"Publishing {src_dir} -> {box_root}" + ("   (DRY RUN)" if args.dry_run else ""))

    failures, kept = [], 0
    raw_jobs = []
    if args.raw:
        raw_path = os.path.expanduser(args.raw)
        if not os.path.isfile(raw_path):
            sys.exit(f"ERROR: raw snapshot not found: {raw_path}")
        if not raw_path.lower().endswith(".json"):
            # Cheap guard: everything in 120-raw is archived as pull_<UTC>.json,
            # so a mistyped --raw would otherwise be filed as a snapshot forever.
            sys.exit(f"ERROR: --raw expects a JSON snapshot, got {raw_path}")
        name = raw_dest_name(raw_path, warn=log)
        dest = os.path.join(raw_dir, name)
        if not os.path.exists(dest):
            rel = os.path.join(args.raw_subdir, name)
            raw_jobs.append((raw_path, dest, rel, rel))
        elif os.path.getsize(dest) == os.path.getsize(raw_path):
            kept += 1
            log(f"  = {os.path.join(args.raw_subdir, name)}  already archived, "
                "left untouched")
        else:
            # Same stamp, different bytes: two different pulls claiming one slot.
            # Overwriting would destroy history, so refuse and let a human look.
            failures.append((name, "a DIFFERENT file is already archived under "
                                   "this name -- refusing to overwrite history"))
            log(f"  ! {args.raw_subdir}/{name}: {failures[-1][1]}")

    clean_jobs, skipped = plan_clean(src_dir, clean_dir)
    for name in skipped:
        log(f"  = {name} skipped (raw snapshot: publish it with --raw)")
    if not clean_jobs:
        log(f"  ! nothing to publish from {src_dir}")

    files, nbytes, errs = run_jobs(raw_jobs + clean_jobs, log, args.dry_run)
    failures.extend(errs)

    verb = "Would copy" if args.dry_run else "Copied"
    log(f"\n{verb} {files} file(s), {human(nbytes)}"
        + (f", {kept} already present" if kept else "")
        + (f", {len(failures)} FAILED" if failures else ""))
    if failures:
        # Always on stderr, even under --quiet: a scheduled publish that fails
        # silently is worse than a noisy one.
        print(f"\n  ! {len(failures)} file(s) did not reach Box:", file=sys.stderr)
        for name, err in failures:
            print(f"      {name}: {err}", file=sys.stderr)
        return 1
    if not args.dry_run:
        log(f"  clean -> {clean_dir}")
        if args.raw:
            log(f"  raw   -> {raw_dir}")
        log("\nBox syncs in the background: check the Box Drive menu bar icon "
            "shows 'Up to date' before sharing the folder.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
