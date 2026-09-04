#!/usr/bin/env python3
"""Build the cin90 results.json payload from a test run.

Runs inside the student repo's workspace, after the test step. Reads:

  --format  pytest-json (default) or junit-xml
  argv[1]   the test report      (default: report.json)
  argv[2]   the console log      (default: pytest_output.txt)

and writes ``results.json`` in the current directory:

  score      = number of passed tests
  max_score  = total number of tests
  output     = a short human-readable summary
  failures   = [{path, line, message}] for each failing test

``results.json`` MUST exist when this script ends: the reporting step reads
it, and a missing file means no payload reaches cin90 at all. The submission
would then sit as "submitted" forever with nobody notified — the exact silent
failure this reporting exists to prevent. Any unexpected crash becomes a
reported error run instead, and if even that cannot be written the script
exits nonzero so the workflow goes red: a visibly failed run is recoverable,
a green one that reported nothing is not.

The "Autograder error:" markers are load-bearing: the server keys
needs-attention state off them, so they must not be reworded casually.

The format is chosen by the caller and never sniffed from the report: the
report is student-controlled output, and letting its contents pick the parser
lets a student pick how they are graded.
"""

import json
import os
import pathlib
import sys

import junit_report

PYTEST_JSON = "pytest-json"
JUNIT_XML = "junit-xml"
FORMATS = (PYTEST_JSON, JUNIT_XML)

# Shown when the console log is missing too, so the run reports the two
# things it knows: no tests, and no output either.
NO_OUTPUT = {
    PYTEST_JSON: (
        "(no pytest output was captured: the failure happened "
        "before pytest ran; check the Actions log)"
    ),
    JUNIT_XML: (
        "(no test output was captured: the failure happened "
        "before the tests ran; check the Actions log)"
    ),
}

# Tail size: 8 KiB read, 4,000 chars sent. The server truncates `output` at
# 50,000 chars anyway, and the AI prompt is bounded on its side; this keeps a
# runaway log from bloating the payload.
TAIL_BYTES = 8192
TAIL_CHARS = 4000

# Notebook conversions an earlier workflow step refused, one per line. Under
# RUNNER_TEMP, not in the checkout, so a student cannot commit this file.
BLOCKED_FILE = os.path.join(
    os.environ.get("RUNNER_TEMP") or ".", "autograde_conversion_errors"
)
# Capped so a repo full of colliding notebooks cannot push the pytest tail
# past the server's 50,000-char truncation of `output`.
BLOCKED_CHARS = 2000


def blocked_conversions():
    """The refused conversions, or "" when there were none.

    Missing file is the normal case. Any other read failure costs us this
    note and nothing else — the grade still reports, so the handler has to be
    as wide as that promise.

    Decoding is lossy: these lines carry student file names, and a path is
    bytes on Linux. Strict UTF-8 raised UnicodeDecodeError, which is not an
    OSError, so an undecodable notebook name failed the whole build and
    replaced a real score with 0/0.

    Overflow is COUNTED, not dropped in silence: lines can be lost to the
    cap, but the fact that they were cannot.
    """
    try:
        raw = pathlib.Path(BLOCKED_FILE).read_bytes()
    except Exception:
        return ""
    text = raw.decode("utf-8", errors="replace").strip()
    if len(text) <= BLOCKED_CHARS:
        return text
    kept = text[:BLOCKED_CHARS].rsplit("\n", 1)[0]
    dropped = len(text.splitlines()) - len(kept.splitlines())
    # "truncated" unconditionally: one path long enough to blow the cap on
    # its own drops no whole entries, and a bare "0 more" would read as a
    # complete list.
    return kept + f"\n(truncated; {dropped} more blocked conversions not shown)"


def console_tail(log_path):
    """Last few KiB of the test runner's console log, or "" if unavailable.

    Best-effort enrichment ONLY. Seeks to the end rather than reading the
    whole file: an import-time print loop can leave a multi-GB log, and
    loading it would raise MemoryError. Decoding is lossy on purpose
    (student code can print raw bytes). Any failure here must cost us the
    traceback, never the payload.
    """
    try:
        with open(log_path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - TAIL_BYTES))
            raw = fh.read()
        return raw.decode("utf-8", errors="replace")[-TAIL_CHARS:].strip()
    except Exception:
        return ""


def _outside(relative):
    """Does this `relpath` result leave the directory it was taken against?

    A path segment may itself begin with `..`, so a prefix test alone reads
    `..data/test_a.py` — which is inside — as a library frame and throws its
    line away.
    """
    return relative == os.pardir or relative.startswith(os.pardir + os.sep)


def _pytest_location(crash, nodeid_path, workspace):
    """`(path, line)` for one pytest failure, resolved against the checkout.

    pytest-json-report normally writes `crash.path` as the runner's absolute
    path, which GitHub's reviews API refuses. A crash resolving outside the
    checkout is a library frame: the nodeid's path stands in and the line is
    dropped, because that line numbers a different file and a review comment
    on the wrong line is worse than one that was never anchored.

    The nodeid fallback is the weaker answer: pytest builds it against its own
    rootdir, which is the repository root only while the starter runs pytest
    from there.
    """
    path = crash.get("path") or ""
    try:
        line = int(crash.get("lineno") or 0)
    except (TypeError, ValueError):
        line = 0
    if not path:
        return nodeid_path, line
    # The report is student-controlled. A non-string raised out of
    # `os.path.isabs` and turned a real 1/2 into a 0/0 "Autograder error:"
    # run, which cin90 records as no grade at all. Coercing it would anchor on
    # whatever it stringifies to, so it goes to the fallback instead.
    if not isinstance(path, str):
        return nodeid_path, 0
    if os.path.isabs(path):
        path = os.path.relpath(
            os.path.realpath(path), os.path.realpath(workspace)
        )
    # Both branches: a relative path can escape the checkout too, and the
    # reviews API refuses `../shared/test_x.py` exactly as it refuses an
    # absolute one.
    if _outside(path):
        return nodeid_path, 0
    return pathlib.PurePath(path).as_posix(), line


def from_pytest_json(report_path, workspace=None):
    """`(total, passed, failures, problem, notes)` from a pytest JSON report.

    `workspace` is the checkout an absolute crash path is resolved against.
    """
    checkout = workspace or "."
    problem = None
    try:
        report = json.loads(pathlib.Path(report_path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        # No usable report. A collection error normally still writes one
        # (with an empty `tests` list), so reaching here means something
        # harder killed the run: a segfault, an OOM kill, or pytest failing
        # to start at all.
        report = {"tests": [], "summary": {}}
        problem = (
            "pytest produced no usable report (it may have crashed or been killed)."
        )

    tests = report.get("tests", [])
    total = len(tests)
    passed = sum(1 for t in tests if t.get("outcome") == "passed")

    failures = []
    unanchored = 0
    for test in tests:
        if test.get("outcome") == "passed":
            continue
        call = test.get("call") or {}
        crash = call.get("crash") or {}
        # nodeid looks like "tests/test_x.py::test_name"
        nodeid_path = test.get("nodeid", "").split("::", 1)[0]
        path, line = _pytest_location(crash, nodeid_path, checkout)
        if crash.get("lineno") and not line:
            unanchored += 1
        failures.append({
            "path": path,
            "line": line,
            "message": crash.get("message") or test.get("outcome", "failed"),
        })

    notes = []
    if unanchored:
        # Not an "Autograder error:" line: the run has a real score. cin90
        # keeps only failures carrying both a path and a line, so a dropped
        # anchor removes the failure from the inline review outright - and it
        # never reaches the API, so the "inline review failed" warning that is
        # the feature's only failing surface does not fire either. Without
        # this the run is indistinguishable from one where nothing failed.
        notes.append(
            f"Note: {unanchored} failure{'' if unanchored == 1 else 's'} "
            "could not be anchored to a file in this repository, so no inline "
            "review comment was written for "
            f"{'it' if unanchored == 1 else 'them'}. The failure text is below."
        )
    return total, passed, failures, problem, notes


def from_junit_xml(report_path, workspace=None):
    """`(total, passed, failures, problem, notes)` from JUnit XML reports.

    A report we cannot read is a 0/0 error run rather than a score over the
    files we could read: a denominator quietly short by one test class is a
    wrong grade nobody would ever notice.
    """
    try:
        total, passed, failures, notes, found = junit_report.parse(
            report_path, workspace
        )
    except junit_report.JunitReportError as exc:
        return 0, 0, [], f"the JUnit XML report could not be read ({exc}).", []

    problem = None
    if not found:
        problem = f"no JUnit XML report was found at {report_path!r}."
    return total, passed, failures, problem, notes


def build_results(report_path, log_path, report_format=PYTEST_JSON, workspace=None):
    # Resolved here rather than in each reader: this is the only caller of
    # either, and two copies of the fallback drift.
    workspace = workspace or os.environ.get("GITHUB_WORKSPACE") or "."
    if report_format == PYTEST_JSON:
        total, passed, failures, problem, notes = from_pytest_json(
            report_path, workspace
        )
    elif report_format == JUNIT_XML:
        total, passed, failures, problem, notes = from_junit_xml(report_path, workspace)
    else:
        raise ValueError(
            f"unknown report format {report_format!r}; expected one of "
            + ", ".join(FORMATS)
        )

    output = f"{passed}/{total} tests passed"
    if total == 0:
        # "0/0 tests passed" reads like a clean run. Say what actually
        # happened instead, and carry the traceback when we have one. The
        # server keys "needs attention" off the assignment, not off this
        # text, but the "Autograder error:" marker is what tells it this
        # repo runs the CURRENT template. The server looks for it ANYWHERE
        # in `output`, not as a prefix — the notes below are prepended ahead
        # of it.
        headline = "Autograder error: " + (problem or "no tests were collected.")
        output = headline + "\n\n" + (console_tail(log_path) or NO_OUTPUT[report_format])

    if notes:
        output = "\n".join(notes) + "\n\n" + output

    blocked = blocked_conversions()
    if blocked:
        # First, not last: a passing run reads "3/3 tests passed" and an
        # instructor who stops at the first line must still see it.
        output = "Notebook conversion blocked:\n" + blocked + "\n\n" + output

    return {
        "score": passed,
        "max_score": total,
        "output": output,
        "failures": failures,
    }


def write_results(payload):
    pathlib.Path("results.json").write_text(json.dumps(payload))


def parse_args(argv):
    """`(report_path, log_path, format)` from the script's arguments.

    Hand-rolled rather than argparse because argparse exits the process on an
    argument it dislikes, and the one thing this script promises is that
    results.json exists when it ends — including when it was called wrongly.
    An unknown format therefore travels on to `build_results`, which reports
    it as an error run.
    """
    positional = []
    report_format = PYTEST_JSON
    rest = list(argv)
    while rest:
        arg = rest.pop(0)
        if arg == "--format":
            report_format = rest.pop(0) if rest else ""
        elif arg.startswith("--format="):
            report_format = arg.split("=", 1)[1]
        else:
            positional.append(arg)

    report_path = positional[0] if positional else "report.json"
    log_path = positional[1] if len(positional) > 1 else "pytest_output.txt"
    return report_path, log_path, report_format


def main(argv):
    report_path, log_path, report_format = parse_args(argv[1:])

    try:
        results = build_results(report_path, log_path, report_format)
    except Exception as exc:
        results = {
            "score": 0,
            "max_score": 0,
            "output": f"Autograder error: could not build results.json ({exc!r}).",
            "failures": [],
        }

    try:
        write_results(results)
    except Exception as exc:
        # The write itself can fail: disk full, permissions, a broken
        # filesystem. Shout in the Actions log, then try once more with a
        # minimal payload.
        print(f"::error::autograder could not write results.json: {exc!r}")
        try:
            write_results({
                "score": 0,
                "max_score": 0,
                "output": f"Autograder infrastructure failure: {exc!r}",
                "failures": [],
            })
        except Exception as exc2:
            # Nothing left to try. Exit nonzero so the workflow goes red.
            print(f"::error::autograder fallback write also failed: {exc2!r}")
            return 1

    print(results["output"][:2000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
