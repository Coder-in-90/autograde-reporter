#!/usr/bin/env python3
"""Build the cin90 results.json payload from a pytest run.

Runs inside the student repo's workspace, after the test step. Reads:

  argv[1]  the pytest JSON report   (default: report.json)
  argv[2]  the pytest console log   (default: pytest_output.txt)

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
"""

import json
import os
import pathlib
import sys

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


def pytest_tail(log_path):
    """Last few KiB of the pytest console log, or "" if unavailable.

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


def build_results(report_path, log_path):
    report_missing = False
    try:
        report = json.loads(pathlib.Path(report_path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        # No usable report. A collection error normally still writes one
        # (with an empty `tests` list), so reaching here means something
        # harder killed the run: a segfault, an OOM kill, or pytest failing
        # to start at all.
        report = {"tests": [], "summary": {}}
        report_missing = True

    tests = report.get("tests", [])
    total = len(tests)
    passed = sum(1 for t in tests if t.get("outcome") == "passed")

    failures = []
    for test in tests:
        if test.get("outcome") == "passed":
            continue
        call = test.get("call") or {}
        crash = call.get("crash") or {}
        # nodeid looks like "tests/test_x.py::test_name"
        path = test.get("nodeid", "").split("::", 1)[0]
        failures.append({
            "path": crash.get("path") or path,
            "line": int(crash.get("lineno") or 0),
            "message": crash.get("message") or test.get("outcome", "failed"),
        })

    output = f"{passed}/{total} tests passed"
    if total == 0:
        # "0/0 tests passed" reads like a clean run. Say what actually
        # happened instead, and carry the traceback when we have one. The
        # server keys "needs attention" off the assignment, not off this
        # text, but the "Autograder error:" marker is what tells it this
        # repo runs the CURRENT template. The server looks for it ANYWHERE
        # in `output`, not as a prefix — the blocked-notebook note below is
        # prepended ahead of it.
        headline = "Autograder error: no tests were collected."
        if report_missing:
            headline = (
                "Autograder error: pytest produced no usable report "
                "(it may have crashed or been killed)."
            )
        tail = pytest_tail(log_path)
        if not tail:
            tail = (
                "(no pytest output was captured: the failure happened "
                "before pytest ran; check the Actions log)"
            )
        output = headline + "\n\n" + tail

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


def main(argv):
    report_path = argv[1] if len(argv) > 1 else "report.json"
    log_path = argv[2] if len(argv) > 2 else "pytest_output.txt"

    try:
        results = build_results(report_path, log_path)
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
