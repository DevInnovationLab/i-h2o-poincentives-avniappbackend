# Data dictionary — review tables

The two CSV files written by the submission-review UI
(`2-code/21-wrangling/review_app.py`, run as `python3 Main.py review`) into Box
`12-avni/123-reviews/`. The code that emits them is
`2-code/20-programs/avni_pipeline/review.py`; `LOG_COLUMNS` and `STATUS_COLUMNS`
there are the authoritative column lists.

They follow the [encoding contract](avni-tables.md#encoding-contract) of the
pipeline tables: ISO 8601 UTC datetimes with a trailing `Z`, India calendar
dates, empty cell for missing, `1` / `0` booleans, ids as strings.

---

## What gets reviewed

Cycles follow **Avni's rolling schedule**: the next survey falls due 5 days
(`--cycle-days`) after the previous one was submitted, not on a fixed grid. All
dates are India calendar dates.

- **Cycles.** Cycle 1 is due on the pump's first survey due date
  (`pump_schedule.first_survey_due_date`). A cycle due on *D* accepts surveys up
  to *D* + 4. Its window opens with no lower bound for cycle 1 (an early first
  survey counts), the day after the previous cycle's submission date if that
  cycle was submitted, or on *D* itself if it was missed.
- **Which surveys belong to a cycle.** The earliest survey in the window fulfils
  the cycle on its date *S*. More surveys on the same date *S* are extra
  submissions in that cycle; a survey on a later date belongs to the next cycle
  and fills it early. Every completed survey lands in exactly one cycle.
- **Next due date.** *S* + 5 if the cycle was submitted (but never on or
  before *D*: a first survey 5 or more days early moves the next due date to
  *D* + 1 at the earliest); *D* + 5 if it was missed. A cycle whose window was still open on the pull date is in progress
  and is the pump's last. Cycles are listed while their due date is on or before
  the pull date, plus one more if a survey has already filled it early.
- **The cycle is the unit of review**: one decision per pump per cycle, however
  many surveys came in.
- **Timeliness**: `on_time` if the survey came in on or before the due date,
  `overdue` if after it (`days_late` = *S* − *D*), `missed` if nothing came in
  and the window closed before the pull date, `in_progress` if nothing has come
  in yet and the window was still open on the pull date. Every cycle is
  reviewable, in-progress ones included.
- **Payment.** A reviewer approves or rejects a cycle's payment with a reason
  (required to reject), and can then mark an approved payment as processed.
- **Outside the cycles**, reviewed in the app and logged, but not in the review
  sheet: grievance and payment-issue forms on a pump.
- **Review periods.** Items are grouped into periods reviewed on the 13th and
  27th of each month (India time). A cycle belongs to the period its due date
  falls in, start inclusive: the period reviewed on the 13th covers due dates
  from the 27th of the previous month to the 12th; the one reviewed on the 27th
  covers the 13th to the 26th.
- **"Now" is the pull date** (`pump_schedule.as_of_date`), never the wall clock,
  so the same snapshot always gives the same cycles.

Items are keyed by `item_id`: `cycle:<subject_id>:<due date>` for a cycle, the
Avni `encounter_id` for an item outside the cycles. Because due dates follow the
submission history, a re-pull in which a survey was edited, back-dated or voided
can move a cycle's due date; a decision logged under the old `item_id` then
matches no item. The log keeps it, the app counts such items at startup and
shows a warning line, and they are left out of the review sheet.

---

## `review_log.csv`

**Grain: one row per event.** Append-only; the system of record. Encoding
follows the pipeline contract (UTC datetimes, ISO dates).

| Column | dtype | Meaning |
|---|---|---|
| `event_id` | string | Random hex id of this event. |
| `recorded_at` | datetime (UTC) | When the event was recorded. |
| `item_id` | string | The reviewed item; see above. |
| `item_kind` | factor | `cycle` \| `other_form`. (`before_schedule` appears only in events recorded before cycles followed Avni's rolling schedule.) |
| `decision` | factor | `approved` \| `rejected` \| `pending` (a reset: withdraws the decision and the payment flag) \| `payment_processed` \| `payment_not_processed`. |
| `reason` | string | Remarks / issues. Required for `rejected`. |
| `reviewer` | string | Name the reviewer typed into the UI. Not authenticated. |
| `review_period` | date | Review date (13th or 27th) of the item's period. |
| `subject_id`, `subject_name`, `village` | | The pump, denormalised at the time of the event. |
| `cycle_due_date` | date | Due date of the cycle; blank outside the cycles. |
| `timeliness` | factor | `on_time` \| `overdue` \| `missed` \| `in_progress` at the time of the event; blank outside the cycles. |
| `n_submissions` | int | Surveys in the cycle at the time of the event. |
| `encounter_ids` | string list | Pipe-joined Avni ids of those surveys. |
| `submitted_by` | string list | Pipe-joined distinct logins that submitted them. |

---

## `review_status.csv` — the review sheet

**Grain: one row per pump cycle**, reviewed or not, every period, laid out
exactly as the team's Google Sheet. Rebuilt from the log on every event and on
app startup. Rows are numbered in order of submission date (the due date for a
cycle with no survey). New cycles sort last, but a row's Log ID can change when
an in-progress cycle is submitted or a re-pull changes the history. **This file deliberately breaks the pipeline's
encoding contract** to match the sheet: `Yes` / `No` values and `16-Oct-2026`
dates.

| Column | Meaning |
|---|---|
| `Log ID` | Row number, 1.., ordered by `Submission Date`, then village and pump. |
| `PO / Village` | The pump's village. |
| `Submission Date` | Date of the survey that fulfilled the cycle; the cycle's due date if none did. |
| `Next Submission Date` | Due date of the next cycle: submission date + 5 days if submitted, due date + 5 days if not. |
| `Login ID used` | Logins that submitted in the cycle (`submitted_by`), comma-separated; blank if none. |
| `Number of submissions` | Surveys in the cycle; blank if none. |
| `Submitted?` | `Yes` if any survey came in during the cycle, else `No`. |
| `Payment Approved?` | `Yes` approved, `No` rejected, blank until reviewed. |
| `Reviewed by` | Reviewer of the current decision. |
| `Review date` | India date of the current decision. |
| `Payment processed?` | `Yes` once marked processed (only possible after approval), else `No`. |
| `Remarks/Issues` | The reason recorded with the current decision. |
