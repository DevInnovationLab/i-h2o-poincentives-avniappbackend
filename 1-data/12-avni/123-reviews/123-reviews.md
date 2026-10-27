# 123-reviews

Reviewer decisions on submissions, written by the review UI
(`python3 Main.py review`). **The one folder under `12-avni/` that holds human
input rather than derived data.**

The pipeline never writes here, and `121-clean/` is never used for this,
because `121-clean/` is overwritten on every run. Decisions are keyed on the
pump and cycle due date for a cycle, and on the Avni `encounter_id` for a form
outside the cycles. Form ids are stable across pulls. Cycle due dates follow
Avni's rolling schedule (next due = submission + 5 days), so an edited or
back-dated survey in a later pull can move a cycle and detach a decision from
it; the decision stays in the log, and the app reports how many are detached.

## Files

| File | Grain | Rule |
|---|---|---|
| `review_log.csv` | one row per decision event | **Append-only.** The system of record. A changed mind is a new row, never an edit. |
| `review_status.csv` | the review sheet: one row per pump per 5-day cycle | **Rebuilt on every decision** from the log plus `121-clean/`. The file to open in Excel or import into Google Sheets. Safe to delete; it is regenerated. |

Column-level documentation lives in
`4-documentation/41-data-dictionaries/review-tables.md`.

## Editing by hand

Do not edit `review_log.csv` in a spreadsheet and save it back: Excel and
Sheets reformat timestamps and ids. To change a decision, record a new one in
the UI. `review_status.csv` can be opened, filtered and copied freely; it is a
view, and any edits to it are lost on the next decision.

## Box path

```
<AVNI_BOX_ROOT>/123-reviews/
```

**Box is where this folder lives.** The UI writes straight to
`<AVNI_BOX_ROOT>/123-reviews/` by default, whichever tables it is reading, and
refuses to start if Box Drive is not mounted. The local `1-data/12-avni/123-reviews/`
is normally empty. `Main.py box` does not publish this folder; it does not need to.

Box Drive syncs files, it does not merge them. If two people record decisions
at the same moment from different machines, Box keeps one `review_log.csv` and
saves the other as a conflict copy. Review one village per person, or take
turns, and check for conflict copies in this folder if a decision seems lost.

## Privacy

Both files carry village names, pump labels, operator usernames and reviewer
free text. Like everything under `1-data/`, they are gitignored and must never
be committed.
