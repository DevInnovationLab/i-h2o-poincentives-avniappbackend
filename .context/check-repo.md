# check-repo

Two checklists and a list of things that are already known to be broken.

**This repository is public.** The scans below are the reason it can be. Run
them; do not assume a diff is clean because it "only touched documentation" —
the most likely leak in this repo is a real village name pasted into an
example.

Run everything from the repo root.

---

## Before every commit — scoped to the diff

Stage your changes first; these check what is actually staged.

1. **Nothing from `1-data/` is staged, and no `.env`.**

   ```bash
   git diff --cached --name-only | grep -E '^1-data/|(^|/)\.env$' && echo "STOP" || echo "ok"
   ```

   Anything printed before `STOP` must be unstaged. If you had to use
   `git add -f` to get it there, that is the bug.

2. **No secrets in the staged content.** JWTs (Avni tokens are Cognito id
   tokens and always start `eyJ`), plus the obvious password/key spellings.

   ```bash
   git diff --cached -U0 \
     | grep -nE 'eyJ[A-Za-z0-9_-]{20,}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+' \
     && echo "STOP" || echo "ok"
   git diff --cached -U0 \
     | grep -niE '(password|secret|api[_-]?key|token|client[_-]?id)\s*[=:]\s*\S{8,}' \
     && echo "REVIEW" || echo "ok"
   ```

   The second one fires on documentation too; read the hits rather than
   trusting the exit code.

3. **No real field data in the staged content.** This is the known-real
   vocabulary — village names, operator identifiers, the UAT org suffix.

   **The vocabulary is derived at scan time from the real subject trees in
   Box, never written into this repository.** Hardcoding the names here would
   publish the study's village list in the very file meant to protect it.

   ```bash
   # Build the vocabulary from Box (requires Box access; nothing is committed)
   FIX=~/Library/CloudStorage/Box-Box/12-avni/122-fixtures/subject_trees
   { grep -h '^SUBJECT' "$FIX"/*.txt | sed 's/^SUBJECT *//; s/ *\[.*//'
     grep -ho 'Village=[^,]*\|Gram Panchayat=[^,]*\|Block=[^,]*\|District=[^,]*' \
          "$FIX"/*.txt | cut -d= -f2
     grep -ho 'submittedBy=[^ ]*' "$FIX"/*.txt | cut -d= -f2 | cut -d@ -f1
   } | sed 's/^ *//; s/ *$//' | grep -vE '^$|^None$' | sort -u \
     | grep -vwiE 'admin|test|approver|operator|main|pump|office|new|details|account' \
     | awk 'length >= 5' > "$TMPDIR/pii-vocab.txt"

   git diff --cached -U0 | grep -niFf "$TMPDIR/pii-vocab.txt" \
     && echo "STOP" || echo "ok"
   grep -niE '@po_uat' <(git diff --cached -U0) && echo "STOP" || echo "ok"
   ```

   The `grep -vwiE` filter drops terms that are ordinary English words
   (`admin`, `test`, `operator`, …) and would otherwise match harmless prose;
   `length >= 5` drops fragments short enough to match inside unrelated words.
   Both filters are why this scan is quiet on a clean tree — if you widen them,
   expect false positives rather than assuming a leak.

   If Box is unavailable the scan cannot run. **Treat that as a blocked check,
   not a pass** — say so in the PR rather than skipping it silently.

   If your diff invents an example subject or user, invent a name that is
   obviously synthetic.

4. **Everything staged still compiles.**

   ```bash
   python3 -m compileall -q Main.py 2-code/ && echo "ok"
   ```

   This writes `__pycache__/` directories on disk. They are gitignored, so
   they will not reach a commit, but they are real files -- `find . -name
   __pycache__ -prune -exec rm -rf {} +` if you want the tree clean.

5. **The import root still resolves.**

   ```bash
   python3 -c "import sys; sys.path.insert(0, '2-code/20-programs'); \
     import envfile, avni_auth, avni_pipeline; print('ok')"
   python3 Main.py --help >/dev/null && echo "ok"
   ```

   If this fails after a folder rename, grep for the literal `20-programs`:
   several bootstraps hard-code it. See `AGENTS.md > The import contract`.

---

## Before opening a PR — scoped to the branch

6. **Only permitted file types are tracked anywhere in the repo.**

   ```bash
   git ls-files | grep -vE '\.(py|md)$|(^|/)\.gitkeep$|^\.gitignore$|^\.env\.example$|^requirements\.txt$|^LICENSE$' \
     && echo "STOP" || echo "ok"
   ```

   Nothing should print. A `.csv`, `.html`, `.json`, `.jpg` or `.txt` in that
   output is a data leak, not a formatting issue.

7. **Re-run the secret and PII scans across the whole branch**, not just the
   last commit — a bad blob three commits back is still in the history you are
   about to push.

   ```bash
   git log -p main..HEAD -U0 -- . ':!.context/check-repo.md' \
     | grep -nE 'eyJ[A-Za-z0-9_-]{20,}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+' \
     && echo "STOP" || echo "ok"
   # vocabulary built as in the pre-commit check above
   git log -p main..HEAD -U0 -- . ':!4-documentation/40-data-sources/avni-api.md' \
     | grep -niFf "$TMPDIR/pii-vocab.txt" && echo "STOP" || echo "ok"
   git log -p main..HEAD -U0 -- . ':!4-documentation/40-data-sources/avni-api.md' \
     | grep -niE '@po_uat' && echo "STOP" || echo "ok"
   ```

   `git log -p` walks every commit on the branch, so a bad blob added three
   commits back and deleted in the fourth still shows up -- which a diff of
   `main...HEAD` would miss. It is scoped to what the branch *changed*: do not
   substitute `git grep <rev>`, which searches each commit's entire tree and so
   reports pre-existing hits on every branch regardless of what it touched.

   One file is excluded: `40-data-sources/avni-api.md`, which documents the
   login identifier format and so legitimately contains `@po_uat`. Nothing
   else may. This checklist no longer needs excluding from its own scan,
   because it no longer contains the vocabulary.

   A hit means rewriting the branch, not adding a fix-up commit. In the case
   of a real token, rotate it as well: it is compromised whether or not you
   rewrite.

8. **The working tree is clean of untracked data.**

   ```bash
   git status --porcelain -uall
   ```

   Read every line. Anything under `1-data/` or any `.csv`/`.html` that shows
   up as untracked means `.gitignore` has a hole — fix `.gitignore` before the
   PR, and say so in the PR description.

9. **`.env.example` still matches what the code reads.**

   ```bash
   grep -rhoE '(os\.environ|os\.getenv)[^)]{0,40}' --include='*.py' 2-code/ Main.py \
     | grep -oE 'AVNI_[A-Z_]+' | sort -u
   grep -oE '^AVNI_[A-Z_]+' .env.example | sort -u
   ```

   The first pattern deliberately matches the call, not the quoting, so it
   catches all four spellings: `os.environ.get("X")`, `os.environ["X"]`,
   `os.getenv("X")` and single-quoted variants. Narrowing it to one spelling
   is how a variable goes undocumented.

   The two lists must be identical. A variable in `.env.example` that no code
   reads is a lie that costs the next person an afternoon; a variable the code
   reads that is not in `.env.example` is a silent misconfiguration.

10. **Documentation follows the code.** If you changed an output table, a
    column or an encoding rule, update
    `1-data/12-avni/121-clean/121-clean.md` and the relevant file under
    `4-documentation/41-data-dictionaries/` in the same PR.

---

## Known issues

These are known, understood and deliberately unfixed. Do not "fix" them
without reading why first.

### No credential-free test path

There is no way for a stranger to exercise this code. The offline
`--source fixtures` mode was **removed before publication** because its
fixtures were real field data — genuine village names, operator usernames and
grievance text, checked into what looked like test files. The three remaining
sources all need something you cannot get from a clone:

- `--source api` needs live Avni credentials;
- `--source raw` needs a `_raw.json` snapshot, which is a full PII-bearing
  dump of the dataset;
- `--source csv` needs an already-built output directory, which is the same
  data in tables.

**Fix if needed:** commit a small *synthetic* `_raw.json` — hand-written,
Avni-shaped, a handful of records, no real names — and restore an offline
path from it. That file would be the one deliberate exception to the "no data
in the repo" rule, and it should be reviewed as carefully as any code.

### 80 of 149 referenced field photos are missing from S3

`fetch_media` reports `NoSuchKey` for 80 of the 149 photo URLs referenced by
the tables. Every missing key has a `scheduled_date` before July 2026. This is
a retention purge on the UAT bucket, not a bug in the fetcher: the URLs are
correct, the objects are gone.

**Do not "fix" this.** Do not add retry logic, do not re-derive the URLs, do
not fall back to unsigned requests. The correct behaviour is what the code
already does: record the failure in the media manifest and carry on. If the
photos matter for analysis, they have to be re-collected in the field or
recovered from a backup of the bucket, neither of which is a code change.

### `42-design/pipeline-build-spec.md` is a historical document

It is the original build spec and it is preserved as a record of the decisions
that were made, not as a description of what exists. In particular **§9
specifies an R datastore (`.rds` / `.RData`) that was never built**, and this
repo is now 100% Python by design (`AGENTS.md > Deliberate deviations`). Other
sections have drifted too. Treat it as an explanation of *why* the tables look
the way they do; treat the code and the data dictionaries as the description
of *what* they are.
