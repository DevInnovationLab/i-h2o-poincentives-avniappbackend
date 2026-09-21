# 3-output

Exploratory and presentational work built *on top of* the pipeline's tables.

```
31-notebooks/   notebooks and scratch analysis
```

## What belongs here

Notebooks that read `1-data/12-avni/121-clean/` to look at something. Nothing
here is an input to anything else: the pipeline never reads this folder, and
deleting the whole directory would not change a single output.

## What must never go here

- **The pipeline's HTML report.** `report.html` is not an output artifact, it
  is the entire dataset wrapped in a viewer. It belongs in
  `1-data/12-avni/121-clean/`, and `*.html` is denied globally in
  `.gitignore` to keep it there.
- **Executed notebooks.** A notebook's saved cell outputs contain real rows.
  Clear all outputs before committing, and note that `.ipynb` is not on the
  `.gitignore` allowlist at all — committing one takes a deliberate override,
  which should make you think twice.
- Any `.csv`. Every CSV in this project is record-level data.
