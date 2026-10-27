# 2-code

All the code. Two folders, and the split between them is the point.

```
20-programs/    reusable code   avni_auth.py, envfile.py, avni_pipeline/
21-wrangling/   the CLI programs avni_to_tables.py, fetch_media.py, sync_box.py,
                                 review_app.py
```

## `20-programs/` — the import root

Library code: an Avni API client with Cognito login and token refresh
(`avni_auth.py`), the `.env` loader (`envfile.py`), and the `avni_pipeline`
package that does the actual work (pull, transform, emit CSV, render report,
fetch media).

**This directory is the import root.** Entry scripts add it to `sys.path`;
nothing inside `avni_pipeline/` may. The full rule, and the one legacy
exception in `pull.py`, are in `AGENTS.md > The import contract`.

## `21-wrangling/` — the programs

The four command-line programs. Each exposes `main(argv=None) -> int` so
`Main.py` can dispatch to it, and each is runnable directly for debugging.
Reach for these through `Main.py` in normal use.

## What must never go here

- Data. No CSVs, no JSON snapshots, no photos, no `.env` — the pipeline reads
  and writes `1-data/`, and the `.gitignore` will refuse most of it anyway.
- Credentials of any kind, including "just for a minute" in a literal.
- Real village names, usernames or grievance text in docstrings, examples or
  test fixtures. Invent obviously synthetic values.
- Third-party dependencies beyond `boto3`. This codebase is standard-library
  only by design; `boto3` exists solely for the Cognito login path.
