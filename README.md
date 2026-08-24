# autograde-reporter

The reporting half of the [Coder in 90](https://coderin90.com) Classroom
autograder, as a versioned GitHub Action.

Student repos run their tests, then this action builds the results payload
and POSTs it to the classroom ingest endpoint, authenticated with a
GitHub-signed OIDC token minted for the run. Because the harness lives here
behind a mutable `v1` tag, a fix to grading or reporting reaches every repo
using the action by moving the tag — no rewrite of student repos, no
"instructors must re-copy this file" contract.

## Usage

The thin workflow a student repo needs (this is what cin90 provisioning
writes; an instructor can also paste it by hand):

```yaml
name: Autograde

on:
  push:
  pull_request:

jobs:
  autograde:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      id-token: write
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"
      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install pytest pytest-json-report
          if [ -f requirements.txt ]; then pip install -r requirements.txt; fi
      - name: Run tests
        run: pytest --json-report --json-report-file=report.json > pytest_output.txt 2>&1 || true
      - uses: Coder-in-90/autograde-reporter@v1
```

`permissions: id-token: write` is the whole authentication story: it lets
the reporter ask GitHub to mint a short-lived token naming the repo the run
happened in. It grants nothing over the repo. There is no secret to set, no
key to leak — a student with `push` on their own repo can read every Action
secret it holds, so the grade never rests on a stored value.

### Inputs

| Input | Default | Purpose |
|---|---|---|
| `base-url` | `https://coderin90.com` | cin90 deployment to report to |
| `results` | `report.json` | pytest JSON report path (`--json-report-file`) |
| `pytest-output` | `pytest_output.txt` | captured pytest console output |

### Legacy fallback

When no OIDC token is available — a `pull_request` run from a fork — the
reporter falls back to signing the body with `CLASSROOM_AUTOGRADE_REPO_SECRET`
or `CLASSROOM_AUTOGRADE_SECRET` from the job environment, matching the
historical template. The fallback is chosen only on the *absence* of OIDC,
never on its failure.

## Versioning

`v1` is a **mutable** tag: it moves to the newest release of the reporter,
and that is the design — harness fixes ship by moving it. Pin a commit SHA
instead if you need a reproducible build.

The version anchors:

- `v1.0.0` — initial extraction: results builder + stdlib OIDC uploader,
  behavior-identical to the `autograde.yml` / `report_autograde.py` pair it
  was extracted from.

`scripts/report_autograde.py` is kept byte-identical to the copy in the
classroom template it was extracted from, so the two can be diffed to prove
no drift.
