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
| `format` | `pytest-json` | report format to grade from: `pytest-json` or `junit-xml` |
| `results` | `report.json` | path to the test report — a file, and for `junit-xml` also a directory or a glob |
| `pytest-output` | `pytest_output.txt` | captured console output of the test run |
| `pr-number` | the triggering pull request | override which PR the result attaches to; leave unset unless you mean to |

## Grading a language other than Python

`format: junit-xml` grades any runner that can write a JUnit XML report,
which is every mainstream one. The score is the same shape either way: every
`<testcase>` counts toward `max_score`, and a testcase with no `<failure>`,
`<error>` or `<skipped>` child scores. A skipped test is in the denominator
and not the numerator, matching what the pytest path already does.

Each failing or errored testcase becomes one entry in `failures`, with `path`
from the testcase's `file` attribute (falling back to `classname`), `line`
from its `line` attribute (0 when absent), and `message` from the child's
`message` attribute, its text, or the tag name — whichever is first present.

**The format is never sniffed from the report.** It is student-controlled
output, and choosing the parser from its contents would let a student choose
how they are graded.

**`results` can be a directory or a glob**, and every matching file is
aggregated into one score. This is not a convenience: Surefire and Gradle both
write one XML file per test class, so a single-file path would grade one slice
of a suite and record it as the whole thing. A directory is searched
recursively for `*.xml`; one with no reports in it is the same "no tests were
collected" error run as an empty report, not a crash.

**Set `results` whenever you set `format`.** Its default is `report.json`,
which is the pytest path's file and no runner's XML.

### Worked examples

Maven Surefire:

```yaml
      - run: mvn -B test > test_output.txt 2>&1 || true
      - uses: Coder-in-90/autograde-reporter@v1.3.0
        with:
          format: junit-xml
          results: target/surefire-reports
          pytest-output: test_output.txt
```

Gradle:

```yaml
      - run: ./gradlew test > test_output.txt 2>&1 || true
      - uses: Coder-in-90/autograde-reporter@v1.3.0
        with:
          format: junit-xml
          results: build/test-results/test
          pytest-output: test_output.txt
```

Jest, via [`jest-junit`](https://www.npmjs.com/package/jest-junit):

```yaml
      - run: npx jest --reporters=default --reporters=jest-junit > test_output.txt 2>&1 || true
        env:
          JEST_JUNIT_OUTPUT_FILE: junit.xml
      - uses: Coder-in-90/autograde-reporter@v1.3.0
        with:
          format: junit-xml
          results: junit.xml
          pytest-output: test_output.txt
```

Go, via [`gotestsum`](https://github.com/gotestyourself/gotestsum):

```yaml
      - run: go install gotest.tools/gotestsum@latest
      - run: gotestsum --junitfile junit.xml ./... > test_output.txt 2>&1 || true
      - uses: Coder-in-90/autograde-reporter@v1.3.0
        with:
          format: junit-xml
          results: junit.xml
          pytest-output: test_output.txt
```

CTest:

```yaml
      - run: ctest --test-dir build --output-junit junit.xml > test_output.txt 2>&1 || true
      - uses: Coder-in-90/autograde-reporter@v1.3.0
        with:
          format: junit-xml
          results: junit.xml
          pytest-output: test_output.txt
```

`|| true` in every one of them is deliberate and matches the pytest example:
the grade comes from the report, not from the runner's exit code, and a
composite step that failed would skip the reporting after it. The action's
last step re-fails the check when the grade is failing.

### Reading a hostile report

The XML is written by the student's own test run, so `scripts/junit_report.py`
treats it as untrusted:

- a document declaring a `DOCTYPE` or an `ENTITY` is refused **unparsed**.
  Expat expands internal entities while parsing, so a billion-laughs document
  is already gigabytes of memory by the time any post-parse check could see
  it. `defusedxml` would do this for us and is not installable here — the
  action runs bare `python` on a GitHub-hosted runner, with no pip step and no
  dependency to add.
- reports are capped at 5 MiB each and 500 files per run. An oversized file is
  refused rather than read to the cap, because a truncated document parses
  into a smaller suite and silently lowers the score. Hitting the file cap
  says so in the reported output.
- each failure message is capped at 2,000 characters and says when it was cut.
  A `<failure>` body is conventionally the whole stack trace, where pytest's
  crash message is one line, so a class of failing tests would otherwise be
  megabytes of payload.
- a file that cannot be read voids the whole aggregate rather than dropping
  out of it. A denominator quietly short by one test class is a wrong grade
  nobody would ever notice; a reported error run is one somebody fixes.

Every one of those ends as a reported `0/0` error run with an
`Autograder error:` line, which is what cin90 keys needs-attention state off.
Nothing raises out of the script: `results.json` exists when it ends, always.

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

**`v1.3.0` adds `format: junit-xml` and changes nothing without it.** The
input defaults to `pytest-json`, which is the behaviour every release before
it had, so a repo that moves to this tag and sets nothing grades exactly as it
did. The one visible change on the default path is that `--format` is now
passed to `scripts/build_results.py`; a workflow calling that script directly
still works without it.

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
- `v1.2.0` — a `pull_request` run reports the pushed head sha rather than the
  merge commit (#397).
- `v1.3.0` — `format: junit-xml`, so a class in any language that writes a
  JUnit XML report can be graded. Default unchanged.

## Self-checks

No framework and no runner: each `scripts/test_*.py` is a plain script that
prints how many checks passed and exits non-zero if any did not.

```bash
python scripts/test_build_results.py
python scripts/test_check_grade.py
python scripts/test_report_autograde.py
```

`scripts/report_autograde.py` is kept byte-identical to the copy in the
classroom template it was extracted from, so the two can be diffed to prove
no drift.
