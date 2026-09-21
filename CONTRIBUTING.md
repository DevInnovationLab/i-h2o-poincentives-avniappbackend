# Contributing

Conventions for this repository live in [`AGENTS.md`](AGENTS.md). This file is
how you set up, run and ship work.

## Setup

Python 3.9+ and nothing else, in the usual case.

```bash
git clone git@github.com:DevInnovationLab/i-h2o-poincentives-avniappbackend.git
cd i-h2o-poincentives-avniappbackend
python3 -m venv .venv
source .venv/bin/activate
```

The pipeline is standard-library-only by design, so there is normally nothing
to install. Install the one dependency **only** if you plan to authenticate
with a username and password instead of pasting a token:

```bash
pip install -r requirements.txt   # boto3, for the Cognito login path only
```

Then configure:

```bash
cp .env.example .env
$EDITOR .env
```

`.env` is gitignored and **must never be committed**. It holds a live Avni
token or a live password. If one reaches a commit, rotate the credential — do
not just amend the commit. `.env.example` documents the only seven variables
the code reads; keep the two in sync when you add or remove one.

`Main.py` loads `.env` for you through `2-code/20-programs/envfile.py`. A real
environment variable always wins over the file, so a one-off token works:

```bash
AVNI_AUTH_TOKEN="$(pbpaste)" python3 Main.py tables --out 1-data/12-avni/121-clean/
```

## Running

`Main.py` is the entry point: it loads `.env` and sets up the import path for
you, so it is what you should reach for. The programs in `2-code/21-wrangling/`
are also runnable directly (they bootstrap their own import path, and the
README's quick start shows this form) -- but then you are responsible for
exporting the environment yourself.

```bash
# pull -> tidy CSV tables + the HTML report
python3 Main.py tables --out 1-data/12-avni/121-clean/ --subject-type Pump

# localise the field photos so the report renders without credentials
python3 Main.py media --dir 1-data/12-avni/121-clean/

# publish the output directory, and archive the snapshot it came from, to Box
python3 Main.py box --dir 1-data/12-avni/121-clean/ --raw <snapshot>.json
```

Arguments after the sub-command are passed straight through, so
`python3 Main.py tables --help` shows the real options.

Everything the pipeline writes lands under `1-data/`, which is entirely
gitignored. That is deliberate: see `AGENTS.md > Data privacy`.

## Branching

- Branch from `main`. **Never commit directly to `main`.**
- Kebab-case branch names that say what the branch does:
  `fix-encounter-attribution`, `add-payment-rate-table`,
  `docs-avni-auth-notes`.
- One concern per branch. A branch that renames things *and* changes behaviour
  is a branch nobody can review.

## Commit messages

```
Short imperative subject, about 50 characters

Body wrapped at 72 characters, explaining why rather than what. The diff
already says what changed; it does not say what you were trying to fix, or
what you tried first that did not work.

Reference issues at the end.
```

- Subject in the imperative mood ("Add", "Fix", "Rename"), not past tense.
- No trailing period on the subject.
- Never put a token, a password, a village name or an operator username in a
  commit message. Commit messages are permanent and this repo is public.

## Before opening a PR

Run the checklists in [`.context/check-repo.md`](.context/check-repo.md) — the
pre-commit one against your diff, the pre-PR one against your whole branch.
They include the secret and PII scans that keep this repo publishable. A PR
that has not had them run is not ready for review.

Describe in the PR what you changed and how you verified it. If you changed
anything the pipeline emits, say whether the CSV table names changed — they are
code constants, and renaming one breaks the re-render path.
