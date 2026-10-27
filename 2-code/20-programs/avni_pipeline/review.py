#!/usr/bin/env python3
"""
review.py
---------
The data half of the submission-review UI: what there is to review, and the
CSV files that record what a reviewer decided about it.

    121-clean/pump_schedule.csv  encounters.csv  observations_long.csv  media/
        |
        |  build_review()               rolling cycles per pump, review periods
        v
    review UI (21-wrangling/review_app.py)
        |
        |  ReviewStore.record()         approve / reject / reset, payment processed
        v
    123-reviews/review_log.csv          append-only, one row per event
    123-reviews/review_status.csv       the review sheet: one row per pump cycle

THE REVIEW MODEL
----------------
Cycles follow Avni's rolling schedule: the next survey is due CYCLE_DAYS after
the previous one was submitted, not on a fixed grid. Per pump, on India
calendar dates:

- Cycle 1 is due on the pump's first survey due date (the
  ``first_survey_due_date`` column of pump_schedule.csv).
- A cycle due on D accepts surveys up to ``D + CYCLE_DAYS - 1``. Its window
  opens with no lower bound for cycle 1 (an early first survey counts), the day
  after the previous cycle's submission date if that cycle was submitted, or
  on D itself if it was missed.
- The earliest survey in the window fulfils the cycle on its date S. Further
  surveys on the same date S are extra submissions in that cycle; a survey on
  a later date belongs to the next cycle (and fills it early).
- The next cycle is due ``S + CYCLE_DAYS`` if this one was submitted (but
  never on or before D, which only a first survey CYCLE_DAYS or more days
  early could cause), and ``D + CYCLE_DAYS`` if it was missed. A cycle still open on the pull date is
  in progress and is the pump's last.

Cycles are generated while their due date is on or before the pull date, plus
one more if a survey has already filled it early. **The cycle is the unit of
review**: one decision per pump per cycle, however many surveys came in. Its
timeliness:

    on_time      submitted on or before the due date
    overdue      submitted after the due date (days_late = S - D)
    missed       no survey, and the window closed before the pull date
    in_progress  no survey yet, and the window was still open on the pull date

Every cycle is reviewable, in-progress ones included. A reviewer approves or
rejects it with a reason, and can then mark an approved cycle's payment as
processed.

Other forms on a pump (grievance, payment issue) sit outside the cycles and
are reviewed in the app only: they are in the log, not in the review sheet.

Items are grouped into review periods that end on the REVIEW_DAYS of each
month, India time. A cycle belongs to the period its due date falls in, start
inclusive: the period reviewed on the 13th covers due dates from the 27th of the
previous month to the 12th; the one reviewed on the 27th covers the 13th to the
26th. Items with no cycle are placed by their submission date.

"Now" is always the pull date (``as_of_date`` in pump_schedule.csv), never the
wall clock, so the same snapshot always gives the same cycles.

THE TWO FILES
-------------
``review_log.csv`` is the system of record. It is only ever appended to, so a
changed mind is a new row rather than an overwrite. ``review_status.csv`` is
the review sheet, laid out for Google Sheets / Excel (SHEET_COLUMNS, Yes/No
values, dd-Mon-yyyy dates) and rebuilt from the log plus the clean tables on
every write: one row per pump cycle, numbered by submission date (the due date
for a cycle with no survey).

Items are keyed by ``item_id``: ``cycle:<subject_id>:<due date>`` for a cycle
and the Avni ``encounter_id`` for an other-form item. Because due dates follow
the submission history, a re-pull in which a survey was edited, back-dated or
voided can move a cycle's due date, and a decision logged under the old
item_id no longer matches any item. ``ReviewStore.orphaned()`` lists such
item_ids so the app can flag them; the log rows themselves are never touched.

``121-clean/`` is rewritten by every pipeline run, so reviews live in a sibling
folder the pipeline never touches. review_app.py reads the Box copy of
121-clean and writes to Box ``12-avni/123-reviews/`` by default.

USAGE
-----
    from avni_pipeline import review

    model = review.build_review("1-data/12-avni/121-clean")
    store = review.ReviewStore("1-data/12-avni/123-reviews")
    store.record(item_id, "rejected", "no survey this week", "reviewer-a", model)
"""

import csv
import datetime
import os
import threading
import uuid

from . import media
from .concepts import SURVEY

LOG_NAME = "review_log.csv"
STATUS_NAME = "review_status.csv"

CYCLE_DAYS = 5
REVIEW_DAYS = (13, 27)            # review dates, days of the month (India time)
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

APPROVED, REJECTED, PENDING = "approved", "rejected", "pending"
PAID, UNPAID = "payment_processed", "payment_not_processed"
# PENDING as an event is a reset: it withdraws the previous decision.
DECISIONS = (APPROVED, REJECTED, PENDING)
EVENTS = DECISIONS + (PAID, UNPAID)

# item kinds
CYCLE_ITEM, OTHER_ITEM = "cycle", "other_form"
# timeliness labels
ON_TIME, OVERDUE, MISSED, IN_PROGRESS = "on_time", "overdue", "missed", "in_progress"

LOG_COLUMNS = ["event_id", "recorded_at", "item_id", "item_kind", "decision", "reason",
               "reviewer", "review_period", "subject_id", "subject_name", "village",
               "cycle_due_date", "timeliness", "n_submissions", "encounter_ids",
               "submitted_by"]

# The review sheet, exactly as the team keeps it in Google Sheets.
SHEET_COLUMNS = ["Log ID", "PO / Village", "Submission Date", "Next Submission Date",
                 "Login ID used", "Number of submissions", "Submitted?",
                 "Payment Approved?", "Reviewed by", "Review date",
                 "Payment processed?", "Remarks/Issues"]

ONE_DAY = datetime.timedelta(days=1)
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _date(text):
    return datetime.date.fromisoformat(text) if text else None


def sheet_date(day):
    """16-Oct-2026, independent of the machine's locale."""
    return f"{day.day:02d}-{MONTHS[day.month - 1]}-{day.year}" if day else ""


# --------------------------------------------------------------------------- #
# Review periods
# --------------------------------------------------------------------------- #

def review_period(day):
    """``(review_date, first_day, last_day)`` of the period ``day`` falls in.

    Start inclusive: a period runs from one review date up to the day before
    the next, and is reviewed on that next date.
    """
    first, second = REVIEW_DAYS
    if day.day < first:
        review = day.replace(day=first)
    elif day.day < second:
        review = day.replace(day=second)
    else:
        nxt = (day.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
        review = nxt.replace(day=first)
    if review.day == second:
        start = review.replace(day=first)
    else:
        prev = review.replace(day=1) - ONE_DAY
        start = prev.replace(day=second)
    return review, start, review - ONE_DAY


# --------------------------------------------------------------------------- #
# Submissions
# --------------------------------------------------------------------------- #

def present_media(clean_dir):
    """File names of the photos actually on disk in ``<clean_dir>/media/``.

    Checks the folder, not ``_media_manifest.csv``. sync_box.py copies into Box
    without deleting, so Box's media/ keeps every photo any earlier build
    downloaded -- including ones since purged from S3, which the latest
    manifest records as failed.
    """
    media_dir = os.path.join(clean_dir, "media")
    if not os.path.isdir(media_dir):
        return set()
    return {name for name in os.listdir(media_dir)
            if not name.startswith(("_", ".")) and not name.endswith(".part")
            and os.path.splitext(name)[1].lower() in media.MEDIA_EXT}


def load_submissions(clean_dir):
    """Every completed encounter in ``clean_dir``, oldest first, with its answers.

    Each submission is a plain dict of the encounter's identifying columns and
    ``answers``: one entry per question in form order, multi-selects joined,
    photo answers resolved to local media file names (``photos``) where the
    file is in media/ and counted as missing (``photos_missing``) where not.
    """
    on_disk = present_media(clean_dir)
    answers = {}
    for obs in _read_csv(os.path.join(clean_dir, "observations_long.csv")):
        by_concept = answers.setdefault(obs["encounter_id"], {})
        entry = by_concept.setdefault(obs["concept_name"], {
            "question": obs["concept_name"], "values": [], "photos": [],
            "photos_missing": 0})
        url = obs.get("media_url") or ""
        if url:
            if media.local_name(url) in on_disk:
                entry["photos"].append(media.local_name(url))
            else:
                entry["photos_missing"] += 1
        else:
            entry["values"].append(obs.get("value_text") or "")

    submissions = []
    for enc in _read_csv(os.path.join(clean_dir, "encounters.csv")):
        if enc.get("status") != "completed" or enc.get("voided") == "1":
            continue
        submissions.append({
            **{k: enc.get(k, "") for k in (
                "encounter_id", "subject_id", "subject_type", "encounter_type",
                "form_version", "fact_table", "submitted_by", "completed_at",
                "completed_date", "last_modified_at")},
            "answers": list(answers.get(enc["encounter_id"], {}).values()),
        })
    submissions.sort(key=lambda s: s["completed_at"])
    return submissions


# --------------------------------------------------------------------------- #
# Cycles and items
# --------------------------------------------------------------------------- #

PUMP_FIELDS = ("subject_id", "subject_name", "village", "gram_panchayat", "district", "state")


def _item(kind, item_id, pump, place_on, submissions, **fields):
    review, start, end = review_period(place_on)
    item = {k: pump.get(k, "") for k in PUMP_FIELDS}
    item.update(item_id=item_id, kind=kind, review_period=review.isoformat(),
                period_start=start.isoformat(), period_end=end.isoformat(),
                cycle_number="", cycle_due_date="", cycle_end_date="",
                next_due_date="", timeliness="", days_late="",
                submissions=submissions)
    item.update(fields)
    return item


def rolling_cycles(start, surveys, as_of, cycle_days=CYCLE_DAYS):
    """The rolling cycles of one pump, oldest first, as plain dicts.

    ``start`` is the first due date, ``surveys`` the pump's completed surveys
    (any order) and ``as_of`` the pull date. Every survey lands in exactly one
    cycle. See the module docstring for the rules.
    """
    step = datetime.timedelta(days=cycle_days)
    surveys = sorted(surveys, key=lambda s: (s["completed_date"], s["completed_at"]))
    cycles, i, n, due = [], 0, len(surveys), start
    # Each window opens where the previous one stopped taking surveys, so the
    # next unassigned survey is always on or after the current window start.
    # Keep going past the pull date only while a survey has already filled the
    # next cycle early.
    while due <= as_of or i < n:
        last = due + step - ONE_DAY
        if i < n and _date(surveys[i]["completed_date"]) <= last:
            done = _date(surveys[i]["completed_date"])
            j = i
            while j < n and _date(surveys[j]["completed_date"]) == done:
                j += 1
            in_cycle, i = surveys[i:j], j
            late = max((done - due).days, 0)
            timeliness = ON_TIME if late == 0 else OVERDUE
            # A first survey CYCLE_DAYS or more days early would put the next
            # due date on or before this one (and reuse its item_id): due
            # dates must strictly increase.
            nxt = max(done + step, due + ONE_DAY)
        else:
            in_cycle, late = [], ""
            timeliness = MISSED if last < as_of else IN_PROGRESS
            nxt = due + step
        cycles.append({"cycle_number": len(cycles) + 1, "cycle_due_date": due,
                       "cycle_end_date": last, "next_due_date": nxt,
                       "timeliness": timeliness, "days_late": late,
                       "submissions": in_cycle})
        if timeliness == IN_PROGRESS:
            break
        due = nxt
    return cycles


def build_review(clean_dir, cycle_days=CYCLE_DAYS):
    """Everything the UI shows: pumps, review periods and review items.

    Needs ``pump_schedule.csv`` (written by the pipeline since the pump
    schedule table was added); raises ``ReviewError`` without it.
    """
    pumps = _read_csv(os.path.join(clean_dir, "pump_schedule.csv"))
    if not pumps:
        raise ReviewError(
            f"no pump_schedule.csv in {clean_dir}: rebuild the tables with the "
            "current pipeline (python3 Main.py tables) and publish them.")
    as_of = _date(pumps[0]["as_of_date"])
    if as_of is None:
        raise ReviewError("pump_schedule.csv has no as_of_date (the pull time is unknown)")

    by_subject = {}
    for sub in load_submissions(clean_dir):
        by_subject.setdefault(sub["subject_id"], []).append(sub)

    items, unscheduled = [], []
    for pump in pumps:
        subs = by_subject.get(pump["subject_id"], [])
        surveys = [s for s in subs if s["fact_table"] == SURVEY]
        for s in subs:
            if s["fact_table"] != SURVEY:
                items.append(_item(OTHER_ITEM, s["encounter_id"], pump,
                                   _date(s["completed_date"]), [s]))

        start = _date(pump["first_survey_due_date"])
        if start is None:
            unscheduled.append({k: pump.get(k, "") for k in PUMP_FIELDS})
            continue
        for c in rolling_cycles(start, surveys, as_of, cycle_days):
            due = c["cycle_due_date"]
            items.append(_item(
                CYCLE_ITEM, f"cycle:{pump['subject_id']}:{due.isoformat()}", pump, due,
                c["submissions"], cycle_number=c["cycle_number"],
                cycle_due_date=due.isoformat(),
                cycle_end_date=c["cycle_end_date"].isoformat(),
                next_due_date=c["next_due_date"].isoformat(),
                timeliness=c["timeliness"], days_late=c["days_late"]))

    def sort_key(i):
        first = (i["submissions"] or [{}])[0]
        return (i["review_period"], i["village"], i["subject_name"], i["subject_id"],
                i["cycle_due_date"] or first.get("completed_date", ""),
                first.get("completed_at", ""))
    items.sort(key=sort_key)

    periods = {}
    for it in items:
        periods.setdefault(it["review_period"], (it["period_start"], it["period_end"]))
    current, cur_start, cur_end = review_period(as_of)
    periods.setdefault(current.isoformat(), (cur_start.isoformat(), cur_end.isoformat()))
    period_list = [{"review_date": r, "start": s, "end": e, "complete": r <= as_of.isoformat()}
                   for r, (s, e) in sorted(periods.items(), reverse=True)]
    complete = [p["review_date"] for p in period_list if p["complete"]]

    return {
        "as_of_date": as_of.isoformat(),
        "cycle_days": cycle_days,
        "review_days": list(REVIEW_DAYS),
        "periods": period_list,
        "default_period": complete[0] if complete else period_list[-1]["review_date"],
        "villages": sorted({p["village"] for p in pumps if p["village"]}),
        "unscheduled_pumps": unscheduled,
        "items": items,
    }


# --------------------------------------------------------------------------- #
# Review store
# --------------------------------------------------------------------------- #

class ReviewError(ValueError):
    """A decision that cannot be recorded (unknown item, missing reason)."""


class ReviewStore(object):
    """The two review CSVs in ``review_dir``. Thread-safe for one process."""

    def __init__(self, review_dir):
        self.review_dir = review_dir
        self.log_path = os.path.join(review_dir, LOG_NAME)
        self.status_path = os.path.join(review_dir, STATUS_NAME)
        self._lock = threading.Lock()
        self.status_ok = True        # did the last write_status() land?
        self._check_log_header()

    def _check_log_header(self):
        if not os.path.exists(self.log_path):
            return
        with open(self.log_path, encoding="utf-8", newline="") as fh:
            header = next(csv.reader(fh), None)
        if header and header != LOG_COLUMNS:
            raise ReviewError(
                f"{self.log_path} has unexpected columns {header}; expected "
                f"{LOG_COLUMNS}. Refusing to append to it.")

    def events(self):
        """Every logged event, oldest first."""
        return _read_csv(self.log_path)

    def current(self):
        """item_id -> {status, reason, reviewer, reviewed_at, payment_processed, history[]}.

        Approve / reject / reset events set the review decision; payment events
        set ``payment_processed`` on their own. A reset clears both.
        """
        state = {}
        for ev in self.events():
            s = state.setdefault(ev["item_id"], {"status": PENDING, "payment_processed": False,
                                                 "history": []})
            s["history"].append({k: ev[k] for k in
                                 ("recorded_at", "decision", "reason", "reviewer")})
            if ev["decision"] in (PAID, UNPAID):
                s.update(payment_processed=ev["decision"] == PAID,
                         payment_by=ev["reviewer"], payment_at=ev["recorded_at"])
            else:
                s.update(status=ev["decision"], reason=ev["reason"],
                         reviewer=ev["reviewer"], reviewed_at=ev["recorded_at"])
                if ev["decision"] != APPROVED:
                    s["payment_processed"] = False
        return state

    def orphaned(self, model):
        """item_ids that have log events but no item in ``model``, sorted.

        Typically a cycle whose due date moved because a later pull changed
        the pump's submission history.
        """
        known = {i["item_id"] for i in model["items"]}
        return sorted({ev["item_id"] for ev in self.events()} - known)

    def record(self, item_id, decision, reason, reviewer, model):
        """Append one event to the log and rebuild the review sheet.

        A rejection must carry a reason; an approval may. Payment can only be
        marked processed on an approved item. Returns the item's new state.
        """
        reason = (reason or "").strip()
        reviewer = (reviewer or "").strip()
        if decision not in EVENTS:
            raise ReviewError(f"decision must be one of {EVENTS}, got {decision!r}")
        if decision == REJECTED and not reason:
            raise ReviewError("a rejection needs a reason")
        if not reviewer:
            raise ReviewError("enter your name as reviewer before recording a decision")
        item = next((i for i in model["items"] if i["item_id"] == item_id), None)
        if item is None:
            raise ReviewError(f"no review item {item_id!r} in the current data")
        if decision == PAID and self.current().get(item_id, {}).get("status") != APPROVED:
            raise ReviewError("approve the payment before marking it processed")

        subs = item["submissions"]
        row = {"event_id": uuid.uuid4().hex, "recorded_at": utc_now(),
               "item_id": item_id, "item_kind": item["kind"], "decision": decision,
               "reason": reason, "reviewer": reviewer,
               "n_submissions": len(subs),
               "encounter_ids": "|".join(s["encounter_id"] for s in subs),
               "submitted_by": "|".join(_unique(s["submitted_by"] for s in subs))}
        row.update({k: item[k] for k in ("review_period", "subject_id", "subject_name",
                                         "village", "cycle_due_date", "timeliness")})
        with self._lock:
            os.makedirs(self.review_dir, exist_ok=True)
            new_file = not os.path.exists(self.log_path) or os.path.getsize(self.log_path) == 0
            with open(self.log_path, "a", encoding="utf-8", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=LOG_COLUMNS, lineterminator="\n")
                if new_file:
                    writer.writeheader()
                writer.writerow(row)
                fh.flush()
                os.fsync(fh.fileno())
            self.status_ok = self.write_status(model) is not None
        return self.current().get(item_id)

    def sheet_rows(self, model):
        """The review sheet: one row per pump cycle, numbered by submission date.

        Submission Date is the date of the survey that fulfilled the cycle, or
        the due date if none did.
        """
        def submission_date(item):
            subs = item["submissions"]
            return subs[0]["completed_date"] if subs else item["cycle_due_date"]

        current = self.current()
        cycles = sorted((i for i in model["items"] if i["kind"] == CYCLE_ITEM),
                        key=lambda i: (submission_date(i), i["village"],
                                       i["subject_name"], i["subject_id"]))
        rows = []
        for n, item in enumerate(cycles, 1):
            st = current.get(item["item_id"], {})
            subs = item["submissions"]
            decided = st.get("status") in (APPROVED, REJECTED)
            reviewed = (datetime.datetime.fromisoformat(st["reviewed_at"].replace("Z", "+00:00"))
                        .astimezone(IST).date() if decided else None)
            rows.append({
                "Log ID": n,
                "PO / Village": item["village"] or item["subject_name"],
                "Submission Date": sheet_date(_date(submission_date(item))),
                "Next Submission Date": sheet_date(_date(item["next_due_date"])),
                "Login ID used": ", ".join(_unique(s["submitted_by"] for s in subs)),
                "Number of submissions": len(subs) or "",
                "Submitted?": "Yes" if subs else "No",
                "Payment Approved?": ("Yes" if st.get("status") == APPROVED else
                                      "No" if st.get("status") == REJECTED else ""),
                "Reviewed by": st.get("reviewer", "") if decided else "",
                "Review date": sheet_date(reviewed),
                "Payment processed?": "Yes" if st.get("payment_processed") else "No",
                "Remarks/Issues": st.get("reason", "") if decided else "",
            })
        return rows

    def write_status(self, model):
        """Rebuild review_status.csv. Returns the path, or None if it is locked.

        Written to a ``.part`` sibling and renamed into place, so a synced
        folder (Box) never uploads a half-written file.
        """
        os.makedirs(self.review_dir, exist_ok=True)
        tmp = self.status_path + ".part"
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=SHEET_COLUMNS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(self.sheet_rows(model))
        try:
            os.replace(tmp, self.status_path)
        except OSError:
            # Typically: open in Excel on Windows. The log already has the
            # decision; the status file catches up on the next write.
            os.remove(tmp)
            return None
        return self.status_path


def _unique(values):
    seen = []
    for v in values:
        if v and v not in seen:
            seen.append(v)
    return seen
