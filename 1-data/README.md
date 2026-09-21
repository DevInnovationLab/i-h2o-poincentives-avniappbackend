# 1-data

A local mirror of the project's Box data folder. **Nothing in here is data this
repository owns** — Box is the system of record, this is a working copy.

```
12-avni/
  120-raw/     append-only raw API snapshots  (pull_<UTC>.json)
  121-clean/   derived tables, report, media/ (rewritten each run)
```

## What belongs here

Only what the pipeline reads and writes: raw snapshots in `120-raw/`, derived
outputs in `121-clean/`. Each folder carries a sidecar `.md` describing what
lives in it.

## What must never go here

- Anything that is not Avni pump data. Other sources get their own numbered
  folder, mirroring Box.
- Anything hand-edited. Everything in `121-clean/` is machine-written and will
  be overwritten on the next run; edit the code, not the output.

## What must never leave here

**Everything in this folder is gitignored except directories, these `.md`
sidecars and `.gitkeep` markers.** The repository is public and the data is
not: real village names, operator usernames, free-text grievances and field
photographs. If you catch yourself typing `git add -f` under `1-data/`, stop
and read `AGENTS.md > Data privacy`.
