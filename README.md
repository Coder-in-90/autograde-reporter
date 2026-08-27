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
| `pr-number` | the triggering pull request | override which PR the result attaches to; leave unset unless you mean to |

### Legacy fallback

When no OIDC token is available — a `pull_request` run from a fork — the
reporter falls back to signing the body with `CLASSROOM_AUTOGRADE_REPO_SECRET`
or `CLASSROOM_AUTOGRADE_SECRET` from the job environment, matching the
historical template. The fallback is chosen only on the *absence* of OIDC,
never on its failure.

### Which commit gets graded

`action.yml` derives the pushed head sha into `CIN90_SHA` and the reporter
prefers it over `GITHUB_SHA`. On a `pull_request` event Actions sets
`GITHUB_SHA` to the throwaway merge commit, so without this the push and
pull_request runs of one commit report different shas, cin90's
per-(submission, sha) dedupe never fires, and the commit is graded and
explained twice (#397).

This has to live in the action: a caller's workflow cannot fix it, because an
`env:` assignment to a `GITHUB_*` variable is
[silently ignored by the runner](https://docs.github.com/en/actions/reference/workflows-and-actions/variables).

## Versioning

`v1` is a **mutable** tag: it moves to the newest release of the reporter,
and that is the design — harness fixes ship by moving it. Pin a commit SHA
instead if you need a reproducible build.

**`v1.2.0` changes which commit a `pull_request` run reports** — the pushed
head rather than the merge commit. A repo on this version stops double-grading
a commit that arrives through a PR. Anything that stored the merge sha from an
earlier release keeps it; nothing rewrites history.

**`v1.1.0` changes the run's conclusion**, which `v1.0.0` never did: a repo that
scores below full marks now ends red instead of green. That is the point (#395),
but it is a behavior change arriving through a moving tag, so anything keying on
a student repo's run conclusion — branch protection, a `needs:` gate, a badge —
starts seeing failures it did not see before. Nothing on the cin90 server does:
`deliver_feedback` reads the reported result, not the conclusion, and there is
no `workflow_run` handler.

Because it moves, a change here reaches every student repo ever provisioned.
Treat the check's exit code as part of the contract: `deliver_feedback` reads
the reported result rather than the run's conclusion, so nothing on the server
depends on it — but a student and an instructor both read the tick.

The version anchors:

- `v1.0.0` — initial extraction: results builder + stdlib OIDC uploader,
  behavior-identical to the `autograde.yml` / `report_autograde.py` pair it
  was extracted from.
- `v1.1.0` — the check now fails when the grade does (#395). Adds
  `scripts/check_grade.py` as a final `if: always()` step, guards the first two
  steps the same way, and derives `PR_NUMBER` from the triggering pull request
  instead of relying on the caller to pass it.

  **This is the first fix distributed by moving `v1`.** Every repo already
  provisioned picks it up on its next run, with nothing to re-copy — which is
  the whole reason the reporter was extracted into an action.

`scripts/report_autograde.py` is kept byte-identical to the copy in the
classroom template it was extracted from, so the two can be diffed to prove
no drift.
