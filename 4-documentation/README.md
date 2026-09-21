# 4-documentation

Prose about the data and the system. Code conventions live in `AGENTS.md`, not
here.

```
40-data-sources/      what Avni is, how its API behaves, how to authenticate
41-data-dictionaries/ column-level documentation of the output tables
42-design/            design notes and the historical build spec
```

## What belongs here

Documentation that outlives a single PR: how the source system works, what a
column means, why the tables are shaped the way they are.

`42-design/` holds historical documents as well as current ones. The build
spec in particular describes decisions, not the present state — see
`.context/check-repo.md > Known issues`.

## What must never go here

- **Real values used as examples.** A data dictionary explains what a column
  contains; it does not need a live village name to do it. The generated
  `data_dictionary.csv` does carry live values in its `example` column, which
  is precisely why that file stays in `1-data/` and is gitignored.
- Credentials, tokens, or anything copied out of a `.env`.
- Screenshots of the report. They are pictures of the dataset.
