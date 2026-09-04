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

### Failure paths

GitHub's reviews API anchors a comment by a path relative to the repository
root. The two readers reach that differently, and only one of them enforces
it.

**pytest** is resolved here. pytest-json-report writes `crash.path` as the
runner's absolute path (`/home/runner/work/<repo>/<repo>/tests/test_x.py`),
which the API refuses, so it is taken relative to `GITHUB_WORKSPACE`. A crash
resolving outside the checkout is a library frame: the failing test's own
nodeid path stands in and the line is dropped, because that line numbers a
different file.

**JUnit** takes the runner's `file` attribute as given, and falls back to
`classname`, which is not a path at all (`com.example.AppTest`). What makes
that work today is that none of the five recipes emits an absolute `file`:
four write no `file` and fall back to `locate()`, which does resolve against
the checkout, and jest-junit's `addFileAttribute` is off. A runner that
started writing one would break the anchor silently.

The nodeid fallback is weaker than a resolved path either way: pytest builds
a nodeid against its own rootdir, which is the repository root only while the
test command runs pytest from there.

### Anchoring a failure that has no file or line

Surefire, Gradle and most JVM and Node runners write **neither** a `file` nor
a `line` attribute — a Java failure arrives as `classname` and line 0. cin90
posts an inline review comment only for a failure carrying both, so without a
line every Java failure would drop out of the inline review and the class
would silently get one summary comment instead. Inline "here is the line that
broke" is the point of the autograder.

The location is in the data, in the trace: `at
com.example.StackTest.testPop(StackTest.java:42)`. So when a testcase has no
`file` attribute, its failure text is scanned for `(Name.ext:LINE)` — one
regex, covering Java, Kotlin, Scala, JavaScript and TypeScript, with no
per-language handling — and the bare filename is resolved against the
checkout the action is running in.

The rules exist to stop a **wrong** anchor, which lands a review comment on an
unrelated line of a student's code:

- the frames are read in order and one naming no file in the checkout is
  skipped rather than given up on. A JUnit assertion failure opens with the
  assertion library's own frames, which are not in the student's repo.
- a filename carried by **two** files is ambiguous and stops the search. The
  failure keeps its `classname` and line 0, exactly as it did before.
- `.git`, `node_modules`, `target` and `build` are skipped, so a build's copy
  of a source file cannot make the real one ambiguous, and the walk stops at
  20,000 files. A walk that hit that cap resolves **nothing**: a filename
  unique only because the rest of the tree went unread is the wrong anchor
  this is trying to avoid.
- a runner that does supply `file` is authoritative, and the trace is never
  consulted for that testcase.

All of it is enrichment: any failure here costs the anchor, never the payload.

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
      - uses: Coder-in-90/autograde-reporter@v1.3.1
        with:
          format: junit-xml
          results: target/surefire-reports
          pytest-output: test_output.txt
```

Gradle:

```yaml
      - run: ./gradlew test > test_output.txt 2>&1 || true
      - uses: Coder-in-90/autograde-reporter@v1.3.1
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
      - uses: Coder-in-90/autograde-reporter@v1.3.1
        with:
          format: junit-xml
          results: junit.xml
          pytest-output: test_output.txt
```

Go, via [`gotestsum`](https://github.com/gotestyourself/gotestsum):

```yaml
      - run: go install gotest.tools/gotestsum@latest
      - run: gotestsum --junitfile junit.xml ./... > test_output.txt 2>&1 || true
      - uses: Coder-in-90/autograde-reporter@v1.3.1
        with:
          format: junit-xml
          results: junit.xml
          pytest-output: test_output.txt
```

CTest:

```yaml
      - run: ctest --test-dir build --output-junit junit.xml > test_output.txt 2>&1 || true
      - uses: Coder-in-90/autograde-reporter@v1.3.1
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

**Unreleased: pytest failure paths are resolved against the checkout.** A
failure whose crash carried the runner's absolute path got no inline comment
from cin90 - the reviews call 422'd and degraded to a summary. A failure
carrying no crash already reported the relative nodeid path and is
unaffected.

**`v1.3.1` anchors JUnit failures that carry no `file` or `line`** by
reading the location out of the stack trace and resolving it against the
checkout. Without it a Java class got no inline review comments at all,
because Surefire writes neither attribute. It only ever fills in a location
that was previously empty, and only when exactly one file in the repo
matches, so no existing anchor changes.

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
- `v1.3.1` — JUnit failures with no `file`/`line` attribute are anchored
  from their stack trace, so Java gets inline review comments.

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
