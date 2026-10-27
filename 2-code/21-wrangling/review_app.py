#!/usr/bin/env python3
"""
review_app.py
-------------
A local web UI for reviewing pump submissions one review period at a time.
Each pump follows Avni's rolling 5-day schedule: the first cycle is due on its
first survey due date, and each later one 5 days after the previous survey was
submitted (or after the previous due date, if that cycle was missed). Every
cycle is one review, showing its surveys (labelled on time or overdue) or a
missed or in-progress slot. A reviewer approves or rejects the cycle's payment
with a reason, then marks approved payments as processed. Decisions whose
cycle no longer exists in the data (a re-pull moved its due date) are counted
at startup and flagged in the page.
Periods end on the 13th and 27th of each month (India time). Every decision is
written to CSV, so it survives restarts and opens directly in Excel or Google
Sheets. The cycle and period rules are in avni_pipeline/review.py.

    121-clean/*.csv  media/        (read; re-read on every page load)
          |
          v
    http://127.0.0.1:8765/         (this server; local machine only)
          |
          v
    123-reviews/review_log.csv     append-only, one row per decision
    123-reviews/review_status.csv  the review sheet, one row per pump cycle

Box is the system of record, so both ends default to Box Drive
($AVNI_BOX_ROOT, as for sync_box.py): the tables come from <box>/121-clean and
the decisions go to <box>/123-reviews, whichever machine runs the app. A fresh
pull is reviewable once it has been published with `Main.py box`. If Box Drive
is not mounted the app refuses to start rather than quietly saving decisions
on one laptop. The pipeline never writes to 123-reviews; see
avni_pipeline/review.py for the file formats.

The server binds to 127.0.0.1 only. It serves real field data -- names, photos,
free text -- so do not expose it on a network interface.

USAGE
-----
    # review two villages (names as they appear in subjects.csv)
    python3 Main.py review --village "Village A" --village "Village B"

    # look at a local build before publishing it (decisions still go to Box)
    python3 Main.py review --dir 1-data/12-avni/121-clean

Options: --port (default 8765), --reviewer (prefills the reviewer name),
--no-browser.
"""

import argparse
import getpass
import json
import mimetypes
import os
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "20-programs"))

from avni_pipeline import review, review_page          # noqa: E402

BOX_ROOT = os.path.expanduser(
    os.environ.get("AVNI_BOX_ROOT", "~/Library/CloudStorage/Box-Box/12-avni"))
CLEAN_SUBDIR = "121-clean"
REVIEWS_SUBDIR = "123-reviews"


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Local web UI to approve or reject submissions, saved to CSV.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--dir", default=None,
                   help="tables folder written by avni_to_tables.py "
                        f"(default: {os.path.join(BOX_ROOT, CLEAN_SUBDIR)})")
    p.add_argument("--reviews", default=None,
                   help="folder for the review CSVs "
                        f"(default: {os.path.join(BOX_ROOT, REVIEWS_SUBDIR)})")
    p.add_argument("--village", action="append", default=[],
                   help="village to show on open; repeat for several "
                        "(default: every village)")
    p.add_argument("--cycle-days", type=int, default=review.CYCLE_DAYS,
                   help="length of a review cycle in days")
    p.add_argument("--reviewer", default=getpass.getuser(),
                   help="prefills the reviewer name in the page")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", dest="browser", action="store_false",
                   help="do not open a browser tab")
    return p.parse_args(argv)


class App(object):
    def __init__(self, clean_dir, review_dir, villages, reviewer, cycle_days):
        self.clean_dir = clean_dir
        self.store = review.ReviewStore(review_dir)
        self.villages = villages
        self.reviewer = reviewer
        self.cycle_days = cycle_days

    def model(self):
        # Re-read every time: a pipeline run between page loads is picked up
        # without restarting, and the tables are small.
        return review.build_review(self.clean_dir, self.cycle_days)

    def state(self):
        manifest = {}
        path = os.path.join(self.clean_dir, "_manifest.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                manifest = json.load(fh)
        model = self.model()
        return {**model,
            "orphaned_reviews": len(self.store.orphaned(model)),
            "meta":{"pulled_at": (manifest.get("source") or {}).get("pulled_at"),
                     "clean_dir": self.clean_dir,
                     "review_dir": self.store.review_dir,
                     "log_path": self.store.log_path,
                     "status_path": self.store.status_path},
            "default_villages": self.villages or model["villages"],
            "default_reviewer": self.reviewer,
            "reviews": self.store.current(),
        }

    def media_file(self, name):
        """Path of a localised photo, only if the media manifest lists it."""
        if name not in review.present_media(self.clean_dir):
            return None
        path = os.path.join(self.clean_dir, "media", name)
        return path if os.path.isfile(path) else None


def make_handler(app, port):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, status, body, ctype, extra=None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status, obj):
            self._send(status, json.dumps(obj).encode("utf-8"), "application/json")

        def _host_ok(self):
            # Refuse requests addressed to any other host name (DNS rebinding).
            if self.headers.get("Host") in allowed_hosts:
                return True
            self._json(403, {"error": "forbidden host"})
            return False

        def do_GET(self):
            if not self._host_ok():
                return
            path = self.path.split("?", 1)[0]
            if path == "/":
                self._send(200, review_page.HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/state":
                try:
                    self._json(200, app.state())
                except review.ReviewError as err:
                    self._json(500, {"error": str(err)})
            elif path == "/export/" + review.STATUS_NAME:
                try:
                    app.store.write_status(app.model())
                except review.ReviewError as err:
                    self._json(500, {"error": str(err)})
                    return
                with open(app.store.status_path, "rb") as fh:
                    body = fh.read()
                self._send(200, body, "text/csv; charset=utf-8", {
                    "Content-Disposition": f'attachment; filename="{review.STATUS_NAME}"'})
            elif path.startswith("/media/"):
                file_path = app.media_file(os.path.basename(path[len("/media/"):]))
                if not file_path:
                    self._json(404, {"error": "no such photo"})
                    return
                with open(file_path, "rb") as fh:
                    body = fh.read()
                ctype = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
                self._send(200, body, ctype)
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._host_ok():
                return
            if self.path != "/api/review":
                self._json(404, {"error": "not found"})
                return
            # A JSON content type forces a CORS preflight, which this server
            # never answers, so other web pages cannot post decisions here.
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                self._json(415, {"error": "expected application/json"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                state = app.store.record(body.get("item_id"), body.get("decision"),
                                         body.get("reason"), body.get("reviewer"),
                                         app.model())
            except (ValueError, review.ReviewError) as err:
                self._json(400, {"error": str(err)})
                return
            warning = None
            if not app.store.status_ok:
                warning = (f"Saved to {review.LOG_NAME}, but {review.STATUS_NAME} could "
                           "not be rewritten (is it open in another program?).")
            self._json(200, {"review": state, "warning": warning})

    return Handler


def main(argv=None):
    args = parse_args(argv)
    if not (args.dir and args.reviews) and not os.path.isdir(BOX_ROOT):
        sys.exit(f"ERROR: Box folder not found: {BOX_ROOT}\n"
                 "Start Box Drive, or set AVNI_BOX_ROOT in .env. Pass both --dir "
                 "and --reviews to run without Box.")
    clean_dir = os.path.abspath(os.path.expanduser(
        args.dir or os.path.join(BOX_ROOT, CLEAN_SUBDIR)))
    if not os.path.exists(os.path.join(clean_dir, "encounters.csv")):
        sys.exit(f"ERROR: no encounters.csv in {clean_dir}\n"
                 "Build the tables first (python3 Main.py tables) or pass --dir.")
    review_dir = os.path.abspath(os.path.expanduser(
        args.reviews or os.path.join(BOX_ROOT, REVIEWS_SUBDIR)))

    if args.cycle_days < 1:
        sys.exit("ERROR: --cycle-days must be at least 1")
    try:
        app = App(clean_dir, review_dir, args.village, args.reviewer, args.cycle_days)
        model = app.model()
    except review.ReviewError as err:
        sys.exit(f"ERROR: {err}")

    for village in args.village:
        if village not in model["villages"]:
            print(f"  ! village {village!r} has no pump in {clean_dir}/pump_schedule.csv "
                  "(names are case-sensitive)")
    status = app.store.write_status(model)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(app, args.port))
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Tables : {clean_dir}")
    print(f"Reviews: {app.store.log_path}")
    print(f"         {status or app.store.status_path + ' (could not write; is it open?)'}")
    orphaned = app.store.orphaned(model)
    if orphaned:
        print(f"  ! {len(orphaned)} reviewed item(s) in {review.LOG_NAME} match no item in "
              "the current data (a re-pull moved the cycle); their decisions are not shown")
    print(f"Serving {url}  (Ctrl-C to stop)")
    if args.browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
