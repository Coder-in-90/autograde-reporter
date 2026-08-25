"""Exit non-zero when the graded submission did not pass (#395).

The workflow runs pytest under `|| true` so that a failing test run still
reaches the reporting step — the score comes from the JSON report, not from
pytest's exit code. Nothing re-raised afterwards, so the job's conclusion was
`success` no matter what the student scored, and the student read the green
tick as "my work passed".

This runs last, after reporting, so making the check honest cannot cost anyone
their grade: whatever happens here, the result has already been delivered.
"""

import json
import pathlib
import sys

RESULTS = "results.json"


def verdict(path):
    """`(exit_code, message)` for the results file at `path`.

    A missing or unreadable file is a failure rather than a pass: the reporting
    step writes one unconditionally, so its absence means the run broke before
    it got there, and "we have no idea" must not read as a green tick.

    `0/0` is a failure too. It is what a collection error produces — no tests
    ran at all — and the server already treats a missing denominator as zero
    rather than full marks.
    """
    file = pathlib.Path(path)
    if not file.exists():
        return 1, f"autograde: no {path}; nothing was graded"
    try:
        results = json.loads(file.read_text())
    except (OSError, ValueError) as exc:
        return 1, f"autograde: could not read {path} ({exc!r})"

    try:
        score = _number(results.get("score", 0))
        max_score = _number(results.get("max_score", 0))
    except (TypeError, ValueError):
        return 1, f"autograde: {path} has a non-numeric score"

    shown = f"{_show(score)}/{_show(max_score)}"
    if max_score <= 0:
        return 1, f"autograde: {shown} — no tests ran, so nothing passed"
    if score < max_score:
        return 1, f"autograde: {shown}"
    return 0, f"autograde: {shown}"


def _number(value):
    """`value` as a float, refusing bools.

    Not int(): truncating would let 1.1 out of 1.9 read as 1/1 and pass. The
    builder writes integer test counts today, but this is the step that decides
    whether a student's work passed, so it does not assume its own input.
    """
    if isinstance(value, bool):
        raise TypeError(f"{value!r} is a bool, not a score")
    return float(value)


def _show(value):
    """Integers without a trailing .0, so 6/6 does not print as 6.0/6.0."""
    return int(value) if float(value).is_integer() else value


def main(argv):
    code, message = verdict(argv[1] if len(argv) > 1 else RESULTS)
    print(message, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
